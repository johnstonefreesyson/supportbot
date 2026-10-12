import os
import math
import time
import uuid
import asyncio
import logging
from collections import defaultdict, deque

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.error import Conflict
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    TypeHandler,
    PicklePersistence,
    PersistenceInput,
    filters,
    ContextTypes,
    ConversationHandler,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# Impede que o token apareça nos logs (as URLs do httpx contêm o token)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ======================================================
# Configuração via variáveis de ambiente (Railway → Variables)
#
#   BOT_TOKEN          (obrigatória) token do bot
#   SUPPORT_URL        (opcional)    link de suporte. Aceita:
#                                    https://t.me/seu_suporte  |  @seu_suporte
#                                    Se não definida, o botão Suporte não aparece.
#   PERSISTENCE_PATH   (opcional)    arquivo .pkl para salvar conversas.
#                                    Se houver um Volume no Railway, o caminho é
#                                    escolhido automaticamente.
#
# NUNCA coloque o token diretamente no código.
# ======================================================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("A variável de ambiente BOT_TOKEN não está definida.")


def normalize_support_url(raw):
    """Converte SUPPORT_URL em um link válido, ou None se não houver/for inválido."""
    if not raw:
        return None
    raw = raw.strip()
    if raw.startswith("@") and len(raw) > 1:
        return f"https://t.me/{raw[1:]}"
    if raw.startswith("t.me/"):
        return "https://" + raw
    if raw.startswith("https://"):
        return raw
    logger.warning("SUPPORT_URL inválida (use https://t.me/... ou @usuario). Ignorada.")
    return None


SUPPORT_URL = normalize_support_url(os.environ.get("SUPPORT_URL"))
if not SUPPORT_URL:
    logger.warning("SUPPORT_URL não definida: o botão Suporte não será exibido.")

GARDEN_URL = "https://chainaid.app/inurl/"

# Estados da conversa
SELECT_ACTION, ENTER_AMOUNT, CONFIRM_ORDER = range(3)

# Taxas
PIX_TO_DEPIX_RATE = 1.0  # 1 Pix = 1 DePix

# Configuração de taxa
FEE_PERCENT = 0.001  # 0.1%
FEE_MIN = 1  # mínimo 1 unidade

# Limites de valor (AJUSTE conforme a sua operação)
MIN_AMOUNT = 10.0
MAX_AMOUNT = 100_000.0

# Cotação
PRICE_CACHE_TTL = 30  # segundos em que o preço em cache é considerado "fresco"
PRICE_MAX_STALE = 300  # idade máxima aceita se a API falhar (5 min)
QUOTE_VALIDITY_SECONDS = 120  # validade da cotação mostrada ao usuário

# Proteção contra excesso de requisições (por usuário)
RATE_LIMIT_MAX = 12  # máximo de interações...
RATE_LIMIT_WINDOW = 60  # ...a cada 60 segundos

# Tempo máximo de inatividade em uma conversa (requer python-telegram-bot[job-queue])
CONVERSATION_TIMEOUT = 600  # 10 minutos

# DePix é atrelado 1:1 ao Real Brasileiro (BRL), então
# 1 DePix = 1 BRL e a taxa DePix → L-BTC é simplesmente 1 / (preço do BTC em BRL).


# ─── Limite de requisições ───────────────────────────────────────────────────

_hits = defaultdict(deque)
_warned = {}


async def rate_limiter(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Bloqueia usuários que enviam interações demais em pouco tempo."""
    user = update.effective_user
    if user is None:
        return

    now = time.monotonic()
    hits = _hits[user.id]
    while hits and now - hits[0] > RATE_LIMIT_WINDOW:
        hits.popleft()

    # Limpeza ocasional para não acumular usuários inativos na memória
    if len(_hits) > 5000:
        for uid in [u for u, h in _hits.items() if not h or now - h[-1] > RATE_LIMIT_WINDOW]:
            _hits.pop(uid, None)
            _warned.pop(uid, None)

    if len(hits) >= RATE_LIMIT_MAX:
        # Avisa no máximo uma vez por janela, para não gerar ainda mais mensagens
        if now - _warned.get(user.id, -RATE_LIMIT_WINDOW) >= RATE_LIMIT_WINDOW:
            _warned[user.id] = now
            try:
                if update.callback_query:
                    await update.callback_query.answer(
                        "Muitas ações em pouco tempo. Aguarde um momento.",
                        show_alert=True,
                    )
                elif update.effective_message:
                    await update.effective_message.reply_text(
                        "⏳ Muitas mensagens em pouco tempo. Aguarde um momento e tente de novo."
                    )
            except Exception as e:
                logger.info(f"Falha ao avisar limite de requisições: {e}")
        raise ApplicationHandlerStop

    hits.append(now)


# ─── Funções da API ──────────────────────────────────────────────────────────

_price_cache = {"price": None, "ts": 0.0}
_price_lock = asyncio.Lock()


async def _fetch_btc_brl():
    """Busca o preço do BTC em BRL na CoinGecko. Retorna float ou None."""
    url = "https://api.coingecko.com/api/v3/simple/price"
    params = {"ids": "bitcoin", "vs_currencies": "brl"}
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as response:
                if response.status != 200:
                    logger.warning(f"API CoinGecko retornou status {response.status}")
                    return None
                data = await response.json()
                price = float(data["bitcoin"]["brl"])
                if not math.isfinite(price) or price <= 0:
                    logger.warning(f"Preço inválido recebido da CoinGecko: {price}")
                    return None
                return price
    except Exception as e:
        logger.error(f"Erro ao buscar preço do BTC: {e}")
        return None


async def get_btc_price_brl():
    """
    Retorna (preço_btc_em_brl, idade_em_segundos) ou None se não houver
    um preço confiável. Usa cache de 30 s para evitar excesso de chamadas.
    Se a API falhar, só usa o último preço bom se tiver menos de 5 min.
    NUNCA devolve um preço inventado.
    """
    async with _price_lock:
        age = time.monotonic() - _price_cache["ts"]
        cached = _price_cache["price"]

        if cached is not None and age <= PRICE_CACHE_TTL:
            return cached, age

        price = await _fetch_btc_brl()
        if price is not None:
            _price_cache["price"] = price
            _price_cache["ts"] = time.monotonic()
            logger.info(f"Preço do BTC obtido: R$ {price:,.2f}")
            return price, 0.0

        if cached is not None and age <= PRICE_MAX_STALE:
            logger.warning(f"API falhou; usando preço em cache de {age:.0f}s atrás")
            return cached, age

        return None


def calculate_depix_to_lbtc_rate(btc_price_brl: float) -> float:
    """
    Taxa DePix → L-BTC.
    L-BTC é atrelado 1:1 ao BTC na Rede Liquid; DePix é atrelado 1:1 ao BRL.
    """
    return 1.0 / btc_price_brl


# ─── Teclados ────────────────────────────────────────────────────────────────

def get_start_keyboard():
    rows = [
        [
            InlineKeyboardButton("Bridge", callback_data="action_bridge"),
            InlineKeyboardButton("Exchange", callback_data="action_exchange"),
        ],
    ]
    if SUPPORT_URL:
        rows.append([InlineKeyboardButton("Suporte", url=SUPPORT_URL)])
    return InlineKeyboardMarkup(rows)


def get_confirm_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ Confirmar – Prosseguir",
                web_app=WebAppInfo(url=GARDEN_URL),
            ),
        ],
        [
            InlineKeyboardButton("❌ Cancelar", callback_data="confirm_no"),
        ],
    ])


def get_back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 Voltar ao Início", callback_data="back_start")]
    ])


def get_expired_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Nova cotação", callback_data="back_start")]
    ])


# ─── Validação ───────────────────────────────────────────────────────────────

def parse_amount(raw: str):
    """
    Converte o texto em float. Retorna (valor, erro).
    Rejeita texto inválido, nan/inf, valores <= 0 e fora dos limites.
    """
    text = raw.strip().replace(",", ".")
    if not text or len(text) > 20:
        return None, "❌ Por favor, digite um número válido (ex: `500`)."

    try:
        amount = float(text)
    except ValueError:
        return None, "❌ Por favor, digite um número válido (ex: `500`)."

    if not math.isfinite(amount) or amount <= 0:
        return None, "❌ Por favor, digite um número positivo válido."

    if amount < MIN_AMOUNT:
        return None, f"❌ O valor mínimo é `{MIN_AMOUNT:,.2f}`. Digite um valor maior."

    if amount > MAX_AMOUNT:
        return None, f"❌ O valor máximo é `{MAX_AMOUNT:,.2f}`. Digite um valor menor."

    return amount, None


# ─── Expiração da cotação ────────────────────────────────────────────────────

async def expire_quote(message, user_data: dict, quote_id: str) -> None:
    """
    Após QUOTE_VALIDITY_SECONDS, troca a mensagem da cotação por um aviso de
    expiração e remove o botão de confirmar (se a cotação ainda for a atual).
    """
    await asyncio.sleep(QUOTE_VALIDITY_SECONDS)

    if user_data.get("quote_id") != quote_id:
        return  # usuário cancelou, voltou ao início ou pediu outra cotação

    user_data.pop("quote_id", None)
    try:
        await message.edit_text(
            "⏰ *Cotação expirada.*\n\n"
            "Os valores mudam com o mercado. Toque em *Nova cotação* para recalcular.",
            parse_mode="Markdown",
            reply_markup=get_expired_keyboard(),
        )
    except Exception as e:
        logger.info(f"Não foi possível marcar a cotação como expirada: {e}")


# ─── Handlers ────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()  # também invalida qualquer cotação pendente

    text = (
        "*O que este bot pode fazer?*\n\n"
        "*Bridge* Pix → DePix ou *Exchange* DePix → L-BTC na menor taxa possível "
        "— sem depender de ninguém.\n\n"
        "Nosso código é aberto. Clique em *Bridge* ou *Exchange* para saber mais e começar.\n\n"
        "Se precisar de ajuda, entre em contato com o *Suporte*."
    )

    if update.message:
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=get_start_keyboard(),
        )
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=get_start_keyboard(),
        )

    return SELECT_ACTION


async def select_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "back_start":
        return await start(update, context)

    action = query.data.replace("action_", "")
    context.user_data["action"] = action

    if action == "bridge":
        await query.edit_message_text(
            "*Bridge – Pix → DePix*\n\n"
            "Qual valor em *Pix* você deseja converter para *DePix*?\n\n"
            f"_(Responda com um número entre {MIN_AMOUNT:,.0f} e {MAX_AMOUNT:,.0f}, "
            "ex: `500`)_",
            parse_mode="Markdown",
            reply_markup=get_back_keyboard(),
        )
    else:
        await query.edit_message_text(
            "*Exchange – DePix → L-BTC*\n\n"
            "Qual valor em *DePix* você deseja trocar por *L-BTC*?\n\n"
            f"_(Responda com um número entre {MIN_AMOUNT:,.0f} e {MAX_AMOUNT:,.0f}, "
            "ex: `500`)_",
            parse_mode="Markdown",
            reply_markup=get_back_keyboard(),
        )

    return ENTER_AMOUNT


async def enter_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    action = context.user_data.get("action", "bridge")

    amount, error = parse_amount(update.message.text)
    if error:
        await update.message.reply_text(
            error,
            parse_mode="Markdown",
            reply_markup=get_back_keyboard(),
        )
        return ENTER_AMOUNT

    # --- Taxa ---
    fee = max(round(amount * FEE_PERCENT, 2), FEE_MIN)
    amount_after_fee = round(amount - fee, 2)

    if amount_after_fee <= 0:
        await update.message.reply_text(
            f"❌ Valor muito baixo após a taxa mínima de {FEE_MIN} unidade(s).\n"
            "Por favor, digite um valor maior.",
            reply_markup=get_back_keyboard(),
        )
        return ENTER_AMOUNT

    # --- Calcular a cotação ---
    stale_note = ""
    if action == "bridge":
        from_unit = "Pix"
        to_unit = "DePix"
        rate = PIX_TO_DEPIX_RATE
        rate_str = "1 Pix ≈ 1 DePix"
        action_label = "Bridge"
        est_time = "~15 min"
    else:
        from_unit = "DePix"
        to_unit = "L-BTC"

        result = await get_btc_price_brl()
        if result is None:
            # Sem preço confiável: não inventamos cotação.
            await update.message.reply_text(
                "⚠️ Não foi possível obter a cotação do BTC agora.\n"
                "Por favor, tente novamente em instantes.",
                reply_markup=get_back_keyboard(),
            )
            return ENTER_AMOUNT

        btc_price_brl, price_age = result
        rate = calculate_depix_to_lbtc_rate(btc_price_brl)
        rate_str = f"1 DePix ≈ {rate:.8f} L-BTC (BTC @ R${btc_price_brl:,.2f})"
        action_label = "Exchange"
        est_time = "~32 min"
        if price_age > PRICE_CACHE_TTL:
            stale_note = f"\n⚠️ Preço atualizado há {price_age:.0f}s (fonte indisponível)."

    you_receive = round(amount_after_fee * rate, 8)

    quote_id = uuid.uuid4().hex
    context.user_data.update({
        "amount": amount,
        "fee": fee,
        "amount_after_fee": amount_after_fee,
        "you_receive": you_receive,
        "from_unit": from_unit,
        "to_unit": to_unit,
        "rate_str": rate_str,
        "action_label": action_label,
        "quote_id": quote_id,
        "quote_ts": time.time(),
    })

    validity_min = QUOTE_VALIDITY_SECONDS / 60
    sent = await update.message.reply_text(
        f"📊 *Resumo do {action_label}*\n\n"
        f"Valor informado: `{amount:.2f}` {from_unit}\n"
        f"Sua taxa (0.1% | mín. {FEE_MIN}): `{fee:.2f}` {from_unit}\n"
        f"Valor repassado: `{amount_after_fee:.2f}` {from_unit}\n"
        f"Taxa: _{rate_str}_\n\n"
        f"Você receberá: ~`{you_receive:.8f}` {to_unit}\n"
        f"Tempo estimado: {est_time}\n"
        f"⏳ Cotação válida por {validity_min:g} min.{stale_note}\n\n"
        f"Toque em *Confirmar* para prosseguir _(abre dentro do Telegram)_, "
        f"ou *Cancelar* para abortar.",
        parse_mode="Markdown",
        reply_markup=get_confirm_keyboard(),
    )

    # Agenda a expiração (application.create_task mantém a referência da tarefa)
    context.application.create_task(
        expire_quote(sent, context.user_data, quote_id)
    )
    return CONFIRM_ORDER


async def confirm_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data.pop("quote_id", None)
    await query.edit_message_text(
        "❌ Ordem cancelada.\n\nDigite /start para iniciar uma nova transação."
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("quote_id", None)
    await update.message.reply_text(
        "❌ Cancelado. Digite /start para iniciar uma nova transação."
    )
    return ConversationHandler.END


# ─── Entradas inesperadas ────────────────────────────────────────────────────

def make_use_buttons_handler(state: int):
    """Resposta para texto digitado onde o bot espera um clique em botão."""
    async def use_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        await update.message.reply_text(
            "Use os botões da mensagem acima ou digite /start para recomeçar."
        )
        return state
    return use_buttons


async def stray_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Texto fora de qualquer conversa ativa."""
    await update.message.reply_text("Digite /start para começar.")


async def stale_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Botão antigo cuja conversa já terminou (timeout, cancelamento, etc.)."""
    await update.callback_query.answer(
        "Esta sessão expirou. Digite /start para recomeçar.", show_alert=True
    )


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Não entendi isso. Digite /start para começar."
    )


# ─── Erros e inicialização ───────────────────────────────────────────────────

async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Registra erros de forma limpa, sem tracebacks repetidos para Conflict."""
    if isinstance(context.error, Conflict):
        logger.warning(
            "Conflict (409): outra instância está usando este token com getUpdates."
        )
        return
    logger.error("Erro não tratado", exc_info=context.error)


async def post_init(app: Application) -> None:
    """Mostra qual bot este serviço está rodando (sem expor o token)."""
    me = await app.bot.get_me()
    logger.info("Iniciado como @%s (id %s)", me.username, me.id)


def get_persistence():
    """
    Salva conversas e dados do usuário em arquivo, para sobreviver a redeploys.
    Requer um Volume no Railway (que define RAILWAY_VOLUME_MOUNT_PATH) ou a
    variável PERSISTENCE_PATH. Sem isso, a persistência fica desativada.
    """
    path = os.environ.get("PERSISTENCE_PATH")
    if not path:
        mount = os.environ.get("RAILWAY_VOLUME_MOUNT_PATH")
        if mount:
            bot_id = BOT_TOKEN.split(":")[0]  # parte pública do token
            path = os.path.join(mount, f"bot_{bot_id}.pkl")

    if not path:
        logger.warning(
            "Persistência desativada (sem Volume/PERSISTENCE_PATH): "
            "as conversas serão perdidas a cada redeploy."
        )
        return None

    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    except OSError as e:
        logger.warning(f"Não foi possível usar {path}: {e}. Persistência desativada.")
        return None

    logger.info("Persistência ativa em %s", path)
    return PicklePersistence(
        filepath=path,
        store_data=PersistenceInput(bot_data=False, chat_data=False, callback_data=False),
        update_interval=30,
    )


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    persistence = get_persistence()

    builder = Application.builder().token(BOT_TOKEN).post_init(post_init)
    if persistence:
        builder = builder.persistence(persistence)
    app = builder.build()

    # Timeout de conversa precisa da JobQueue (pip install "python-telegram-bot[job-queue]")
    if app.job_queue is not None:
        conversation_timeout = CONVERSATION_TIMEOUT
    else:
        conversation_timeout = None
        logger.warning(
            "JobQueue indisponível: timeout de conversa desativado. "
            'Instale com: pip install "python-telegram-bot[job-queue]"'
        )

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            # "Nova cotação" / "Voltar" funcionam mesmo após um timeout ou redeploy
            CallbackQueryHandler(start, pattern="^back_start$"),
        ],
        states={
            SELECT_ACTION: [
                CallbackQueryHandler(select_action, pattern="^action_"),
                CallbackQueryHandler(select_action, pattern="^back_start$"),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    make_use_buttons_handler(SELECT_ACTION),
                ),
            ],
            ENTER_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_amount),
                CallbackQueryHandler(select_action, pattern="^back_start$"),
            ],
            CONFIRM_ORDER: [
                CallbackQueryHandler(confirm_cancel, pattern="^confirm_no$"),
                CallbackQueryHandler(select_action, pattern="^back_start$"),
                MessageHandler(
                    filters.TEXT & ~filters.COMMAND,
                    make_use_buttons_handler(CONFIRM_ORDER),
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
        conversation_timeout=conversation_timeout,
        name="main_conversation",
        persistent=persistence is not None,
    )

    # Grupo -1: roda antes de tudo e bloqueia quem exceder o limite
    app.add_handler(TypeHandler(Update, rate_limiter), group=-1)

    app.add_handler(conv_handler)
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, stray_text))
    app.add_handler(CallbackQueryHandler(stale_button))
    app.add_error_handler(on_error)

    logger.info("Bot está rodando...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
