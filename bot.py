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

# ─── Traduções ───────────────────────────────────────────────────────────────

TRANSLATIONS = {
    "pt": {
        "lang_name": "Português",
        "lang_flag": "🇧🇷",
        "start_text": (
            "*O que este bot pode fazer?*\n\n"
            "*Bridge* Pix → DePix ou *Exchange* DePix → L-BTC na menor taxa possível "
            "— sem depender de ninguém.\n\n"
            "Nosso código é aberto. Clique em *Bridge* ou *Exchange* para saber mais e começar.\n\n"
            "Se precisar de ajuda, entre em contato com o *Suporte*."
        ),
        "btn_bridge": "Bridge",
        "btn_exchange": "Exchange",
        "btn_support": "Suporte",
        "btn_confirm": "✅ Confirmar – Prosseguir",
        "btn_cancel": "❌ Cancelar",
        "btn_back": "🔙 Voltar ao Início",
        "btn_new_quote": "🔄 Nova cotação",
        "bridge_title": (
            "*Bridge – Pix → DePix*\n\n"
            "Qual valor em *Pix* você deseja converter para *DePix*?\n\n"
            "_(Responda com um número entre {min} e {max}, ex: `500`)_"
        ),
        "exchange_title": (
            "*Exchange – DePix → L-BTC*\n\n"
            "Qual valor em *DePix* você deseja trocar por *L-BTC*?\n\n"
            "_(Responda com um número entre {min} e {max}, ex: `500`)_"
        ),
        "invalid_number": "❌ Por favor, digite um número válido (ex: `500`).",
        "invalid_positive": "❌ Por favor, digite um número positivo válido.",
        "min_amount": "❌ O valor mínimo é `{min:,.2f}`. Digite um valor maior.",
        "max_amount": "❌ O valor máximo é `{max:,.2f}`. Digite um valor menor.",
        "amount_too_low": (
            "❌ Valor muito baixo após a taxa mínima de {fee_min} unidade(s).\n"
            "Por favor, digite um valor maior."
        ),
        "quote_error": (
            "⚠️ Não foi possível obter a cotação do BTC agora.\n"
            "Por favor, tente novamente em instantes."
        ),
        "summary_title": "📊 *Resumo do {action}*",
        "summary_amount": "Valor informado: `{amount:.2f}` {from_unit}",
        "summary_fee": "Sua taxa (0.1% | mín. {fee_min}): `{fee:.2f}` {from_unit}",
        "summary_net": "Valor repassado: `{net:.2f}` {from_unit}",
        "summary_rate": "Taxa: _{rate}_",
        "summary_receive": "Você receberá: ~`{receive:.8f}` {to_unit}",
        "summary_time": "Tempo estimado: {time}",
        "summary_validity": "⏳ Cotação válida por {validity:g} min.{stale}",
        "summary_instructions": (
            "Toque em *Confirmar* para prosseguir _(abre dentro do Telegram)_, "
            "ou *Cancelar* para abortar."
        ),
        "stale_note": "\n⚠️ Preço atualizado há {age:.0f}s (fonte indisponível).",
        "quote_expired": (
            "⏰ *Cotação expirada.*\n\n"
            "Os valores mudam com o mercado. Toque em *Nova cotação* para recalcular."
        ),
        "cancelled": "❌ Ordem cancelada.\n\nDigite /start para iniciar uma nova transação.",
        "cancelled_cmd": "❌ Cancelado. Digite /start para iniciar uma nova transação.",
        "use_buttons": "Use os botões da mensagem acima ou digite /start para recomeçar.",
        "start_cmd": "Digite /start para começar.",
        "stale_button": "Esta sessão expirou. Digite /start para recomeçar.",
        "unknown": "Não entendi isso. Digite /start para começar.",
        "rate_limit": "⏳ Muitas mensagens em pouco tempo. Aguarde um momento e tente de novo.",
        "rate_limit_alert": "Muitas ações em pouco tempo. Aguarde um momento.",
    },
    "en": {
        "lang_name": "English",
        "lang_flag": "🇺🇸",
        "start_text": (
            "*What can this bot do?*\n\n"
            "*Bridge* Pix → DePix or *Exchange* DePix → L-BTC at the lowest possible fee "
            "— without relying on anyone.\n\n"
            "Our code is open source. Click *Bridge* or *Exchange* to learn more and get started.\n\n"
            "If you need help, contact *Support*."
        ),
        "btn_bridge": "Bridge",
        "btn_exchange": "Exchange",
        "btn_support": "Support",
        "btn_confirm": "✅ Confirm – Proceed",
        "btn_cancel": "❌ Cancel",
        "btn_back": "🔙 Back to Start",
        "btn_new_quote": "🔄 New Quote",
        "bridge_title": (
            "*Bridge – Pix → DePix*\n\n"
            "What amount in *Pix* do you want to convert to *DePix*?\n\n"
            "_(Reply with a number between {min} and {max}, e.g. `500`)_"
        ),
        "exchange_title": (
            "*Exchange – DePix → L-BTC*\n\n"
            "What amount in *DePix* do you want to exchange for *L-BTC*?\n\n"
            "_(Reply with a number between {min} and {max}, e.g. `500`)_"
        ),
        "invalid_number": "❌ Please enter a valid number (e.g. `500`).",
        "invalid_positive": "❌ Please enter a valid positive number.",
        "min_amount": "❌ The minimum amount is `{min:,.2f}`. Enter a larger value.",
        "max_amount": "❌ The maximum amount is `{max:,.2f}`. Enter a smaller value.",
        "amount_too_low": (
            "❌ Amount too low after the minimum fee of {fee_min} unit(s).\n"
            "Please enter a larger value."
        ),
        "quote_error": (
            "⚠️ Could not fetch the BTC quote right now.\n"
            "Please try again in a moment."
        ),
        "summary_title": "📊 *{action} Summary*",
        "summary_amount": "Amount entered: `{amount:.2f}` {from_unit}",
        "summary_fee": "Your fee (0.1% | min. {fee_min}): `{fee:.2f}` {from_unit}",
        "summary_net": "Amount forwarded: `{net:.2f}` {from_unit}",
        "summary_rate": "Rate: _{rate}_",
        "summary_receive": "You will receive: ~`{receive:.8f}` {to_unit}",
        "summary_time": "Estimated time: {time}",
        "summary_validity": "⏳ Quote valid for {validity:g} min.{stale}",
        "summary_instructions": (
            "Tap *Confirm* to proceed _(opens inside Telegram)_, "
            "or *Cancel* to abort."
        ),
        "stale_note": "\n⚠️ Price updated {age:.0f}s ago (source unavailable).",
        "quote_expired": (
            "⏰ *Quote expired.*\n\n"
            "Values change with the market. Tap *New Quote* to recalculate."
        ),
        "cancelled": "❌ Order cancelled.\n\nType /start to begin a new transaction.",
        "cancelled_cmd": "❌ Cancelled. Type /start to begin a new transaction.",
        "use_buttons": "Use the buttons on the message above or type /start to restart.",
        "start_cmd": "Type /start to begin.",
        "stale_button": "This session has expired. Type /start to restart.",
        "unknown": "I didn't understand that. Type /start to begin.",
        "rate_limit": "⏳ Too many messages in a short time. Please wait a moment and try again.",
        "rate_limit_alert": "Too many actions in a short time. Please wait a moment.",
    },
    "es": {
        "lang_name": "Español",
        "lang_flag": "🇪🇸",
        "start_text": (
            "*¿Qué puede hacer este bot?*\n\n"
            "*Bridge* Pix → DePix o *Exchange* DePix → L-BTC con la menor tasa posible "
            "— sin depender de nadie.\n\n"
            "Nuestro código es abierto. Haz clic en *Bridge* o *Exchange* para saber más y comenzar.\n\n"
            "Si necesitas ayuda, contacta al *Soporte*."
        ),
        "btn_bridge": "Bridge",
        "btn_exchange": "Exchange",
        "btn_support": "Soporte",
        "btn_confirm": "✅ Confirmar – Continuar",
        "btn_cancel": "❌ Cancelar",
        "btn_back": "🔙 Volver al Inicio",
        "btn_new_quote": "🔄 Nueva cotización",
        "bridge_title": (
            "*Bridge – Pix → DePix*\n\n"
            "¿Qué cantidad en *Pix* deseas convertir a *DePix*?\n\n"
            "_(Responde con un número entre {min} y {max}, ej: `500`)_"
        ),
        "exchange_title": (
            "*Exchange – DePix → L-BTC*\n\n"
            "¿Qué cantidad en *DePix* deseas cambiar por *L-BTC*?\n\n"
            "_(Responde con un número entre {min} y {max}, ej: `500`)_"
        ),
        "invalid_number": "❌ Por favor, ingresa un número válido (ej: `500`).",
        "invalid_positive": "❌ Por favor, ingresa un número positivo válido.",
        "min_amount": "❌ El valor mínimo es `{min:,.2f}`. Ingresa un valor mayor.",
        "max_amount": "❌ El valor máximo es `{max:,.2f}`. Ingresa un valor menor.",
        "amount_too_low": (
            "❌ Valor demasiado bajo después de la tasa mínima de {fee_min} unidad(es).\n"
            "Por favor, ingresa un valor mayor."
        ),
        "quote_error": (
            "⚠️ No se pudo obtener la cotización del BTC ahora.\n"
            "Por favor, inténtalo de nuevo en unos momentos."
        ),
        "summary_title": "📊 *Resumen del {action}*",
        "summary_amount": "Valor ingresado: `{amount:.2f}` {from_unit}",
        "summary_fee": "Tu tasa (0.1% | mín. {fee_min}): `{fee:.2f}` {from_unit}",
        "summary_net": "Valor transferido: `{net:.2f}` {from_unit}",
        "summary_rate": "Tasa: _{rate}_",
        "summary_receive": "Recibirás: ~`{receive:.8f}` {to_unit}",
        "summary_time": "Tiempo estimado: {time}",
        "summary_validity": "⏳ Cotización válida por {validity:g} min.{stale}",
        "summary_instructions": (
            "Toca *Confirmar* para continuar _(se abre dentro de Telegram)_, "
            "o *Cancelar* para abortar."
        ),
        "stale_note": "\n⚠️ Precio actualizado hace {age:.0f}s (fuente no disponible).",
        "quote_expired": (
            "⏰ *Cotización expirada.*\n\n"
            "Los valores cambian con el mercado. Toca *Nueva cotización* para recalcular."
        ),
        "cancelled": "❌ Orden cancelada.\n\nEscribe /start para iniciar una nueva transacción.",
        "cancelled_cmd": "❌ Cancelado. Escribe /start para iniciar una nueva transacción.",
        "use_buttons": "Usa los botones del mensaje de arriba o escribe /start para reiniciar.",
        "start_cmd": "Escribe /start para comenzar.",
        "stale_button": "Esta sesión ha expirado. Escribe /start para reiniciar.",
        "unknown": "No entendí eso. Escribe /start para comenzar.",
        "rate_limit": "⏳ Demasiados mensajes en poco tiempo. Espera un momento e intenta de nuevo.",
        "rate_limit_alert": "Demasiadas acciones en poco tiempo. Espera un momento.",
    },
}

DEFAULT_LANG = "pt"


def t(user_data: dict, key: str, **kwargs) -> str:
    """Retorna a string traduzida para o idioma do usuário."""
    lang = user_data.get("lang", DEFAULT_LANG)
    strings = TRANSLATIONS.get(lang, TRANSLATIONS[DEFAULT_LANG])
    text = strings.get(key, TRANSLATIONS[DEFAULT_LANG].get(key, key))
    if kwargs:
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError):
            return text
    return text


# Estados da conversa
SELECT_LANGUAGE, SELECT_ACTION, ENTER_AMOUNT, CONFIRM_ORDER = range(4)

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
                lang = context.user_data.get("lang", DEFAULT_LANG) if context.user_data else DEFAULT_LANG
                if update.callback_query:
                    await update.callback_query.answer(
                        t({"lang": lang}, "rate_limit_alert"),
                        show_alert=True,
                    )
                elif update.effective_message:
                    await update.effective_message.reply_text(
                        t({"lang": lang}, "rate_limit")
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

def get_language_keyboard():
    rows = []
    for code, strings in TRANSLATIONS.items():
        rows.append([
            InlineKeyboardButton(
                f"{strings['lang_flag']} {strings['lang_name']}",
                callback_data=f"lang_{code}",
            )
        ])
    return InlineKeyboardMarkup(rows)


def get_start_keyboard(user_data: dict):
    rows = [
        [
            InlineKeyboardButton(t(user_data, "btn_bridge"), callback_data="action_bridge"),
            InlineKeyboardButton(t(user_data, "btn_exchange"), callback_data="action_exchange"),
        ],
        [
            InlineKeyboardButton(
                f"🌐 {TRANSLATIONS.get(user_data.get('lang', DEFAULT_LANG), TRANSLATIONS[DEFAULT_LANG])['lang_flag']} "
                f"{TRANSLATIONS.get(user_data.get('lang', DEFAULT_LANG), TRANSLATIONS[DEFAULT_LANG])['lang_name']}",
                callback_data="change_language",
            ),
        ],
    ]
    if SUPPORT_URL:
        rows.append([InlineKeyboardButton(t(user_data, "btn_support"), url=SUPPORT_URL)])
    return InlineKeyboardMarkup(rows)


def get_confirm_keyboard(user_data: dict):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                t(user_data, "btn_confirm"),
                web_app=WebAppInfo(url=GARDEN_URL),
            ),
        ],
        [
            InlineKeyboardButton(t(user_data, "btn_cancel"), callback_data="confirm_no"),
        ],
    ])


def get_back_keyboard(user_data: dict):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(user_data, "btn_back"), callback_data="back_start")]
    ])


def get_expired_keyboard(user_data: dict):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(t(user_data, "btn_new_quote"), callback_data="back_start")]
    ])


# ─── Validação ───────────────────────────────────────────────────────────────

def parse_amount(raw: str, user_data: dict):
    """
    Converte o texto em float. Retorna (valor, erro).
    Rejeita texto inválido, nan/inf, valores <= 0 e fora dos limites.
    """
    text = raw.strip().replace(",", ".")
    if not text or len(text) > 20:
        return None, t(user_data, "invalid_number")

    try:
        amount = float(text)
    except ValueError:
        return None, t(user_data, "invalid_number")

    if not math.isfinite(amount) or amount <= 0:
        return None, t(user_data, "invalid_positive")

    if amount < MIN_AMOUNT:
        return None, t(user_data, "min_amount", min=MIN_AMOUNT)

    if amount > MAX_AMOUNT:
        return None, t(user_data, "max_amount", max=MAX_AMOUNT)

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
            t(user_data, "quote_expired"),
            parse_mode="Markdown",
            reply_markup=get_expired_keyboard(user_data),
        )
    except Exception as e:
        logger.info(f"Não foi possível marcar a cotação como expirada: {e}")


# ─── Handlers ────────────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """
    Ponto de entrada: se o usuário já tem idioma salvo, mostra o menu principal.
    Caso contrário, pede para escolher o idioma.
    """
    context.user_data.pop("quote_id", None)

    if context.user_data.get("lang"):
        return await show_main_menu(update, context)

    return await ask_language(update, context)


async def ask_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Mostra a tela de seleção de idioma."""
    text = (
        "🌐 *Choose your language* / *Escolha seu idioma* / *Elige tu idioma*\n\n"
        "Please select your preferred language.\n"
        "Por favor, selecione seu idioma preferido.\n"
        "Por favor, selecciona tu idioma preferido."
    )

    if update.message:
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=get_language_keyboard(),
        )
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=get_language_keyboard(),
        )

    return SELECT_LANGUAGE


async def select_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Salva o idioma escolhido e mostra o menu principal."""
    query = update.callback_query
    await query.answer()

    lang = query.data.replace("lang_", "")
    if lang not in TRANSLATIONS:
        lang = DEFAULT_LANG

    context.user_data["lang"] = lang

    return await show_main_menu(update, context)


async def change_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Permite trocar o idioma a qualquer momento."""
    query = update.callback_query
    await query.answer()
    return await ask_language(update, context)


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Mostra o menu principal no idioma do usuário."""
    text = t(context.user_data, "start_text")
    keyboard = get_start_keyboard(context.user_data)

    if update.message:
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )
    elif update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(
            text,
            parse_mode="Markdown",
            reply_markup=keyboard,
        )

    return SELECT_ACTION


async def select_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()

    if query.data == "back_start":
        return await show_main_menu(update, context)

    if query.data == "change_language":
        return await change_language(update, context)

    action = query.data.replace("action_", "")
    context.user_data["action"] = action

    if action == "bridge":
        await query.edit_message_text(
            t(
                context.user_data,
                "bridge_title",
                min=f"{MIN_AMOUNT:,.0f}",
                max=f"{MAX_AMOUNT:,.0f}",
            ),
            parse_mode="Markdown",
            reply_markup=get_back_keyboard(context.user_data),
        )
    else:
        await query.edit_message_text(
            t(
                context.user_data,
                "exchange_title",
                min=f"{MIN_AMOUNT:,.0f}",
                max=f"{MAX_AMOUNT:,.0f}",
            ),
            parse_mode="Markdown",
            reply_markup=get_back_keyboard(context.user_data),
        )

    return ENTER_AMOUNT


async def enter_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    action = context.user_data.get("action", "bridge")

    amount, error = parse_amount(update.message.text, context.user_data)
    if error:
        await update.message.reply_text(
            error,
            parse_mode="Markdown",
            reply_markup=get_back_keyboard(context.user_data),
        )
        return ENTER_AMOUNT

    # --- Taxa ---
    fee = max(round(amount * FEE_PERCENT, 2), FEE_MIN)
    amount_after_fee = round(amount - fee, 2)

    if amount_after_fee <= 0:
        await update.message.reply_text(
            t(context.user_data, "amount_too_low", fee_min=FEE_MIN),
            reply_markup=get_back_keyboard(context.user_data),
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
                t(context.user_data, "quote_error"),
                reply_markup=get_back_keyboard(context.user_data),
            )
            return ENTER_AMOUNT

        btc_price_brl, price_age = result
        rate = calculate_depix_to_lbtc_rate(btc_price_brl)
        rate_str = f"1 DePix ≈ {rate:.8f} L-BTC (BTC @ R${btc_price_brl:,.2f})"
        action_label = "Exchange"
        est_time = "~32 min"
        if price_age > PRICE_CACHE_TTL:
            stale_note = t(context.user_data, "stale_note", age=price_age)

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
        t(context.user_data, "summary_title", action=action_label) + "\n\n"
        + t(context.user_data, "summary_amount", amount=amount, from_unit=from_unit) + "\n"
        + t(context.user_data, "summary_fee", fee=fee, fee_min=FEE_MIN, from_unit=from_unit) + "\n"
        + t(context.user_data, "summary_net", net=amount_after_fee, from_unit=from_unit) + "\n"
        + t(context.user_data, "summary_rate", rate=rate_str) + "\n\n"
        + t(context.user_data, "summary_receive", receive=you_receive, to_unit=to_unit) + "\n"
        + t(context.user_data, "summary_time", time=est_time) + "\n"
        + t(context.user_data, "summary_validity", validity=validity_min, stale=stale_note) + "\n\n"
        + t(context.user_data, "summary_instructions"),
        parse_mode="Markdown",
        reply_markup=get_confirm_keyboard(context.user_data),
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
    await query.edit_message_text(t(context.user_data, "cancelled"))
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("quote_id", None)
    await update.message.reply_text(t(context.user_data, "cancelled_cmd"))
    return ConversationHandler.END


# ─── Entradas inesperadas ────────────────────────────────────────────────────

def make_use_buttons_handler(state: int):
    """Resposta para texto digitado onde o bot espera um clique em botão."""
    async def use_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        await update.message.reply_text(t(context.user_data, "use_buttons"))
        return state
    return use_buttons


async def stray_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Texto fora de qualquer conversa ativa."""
    await update.message.reply_text(t(context.user_data, "start_cmd"))


async def stale_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Botão antigo cuja conversa já terminou (timeout, cancelamento, etc.)."""
    await update.callback_query.answer(
        t(context.user_data, "stale_button"), show_alert=True
    )


async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(t(context.user_data, "unknown"))


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
            SELECT_LANGUAGE: [
                CallbackQueryHandler(select_language, pattern="^lang_"),
            ],
            SELECT_ACTION: [
                CallbackQueryHandler(select_action, pattern="^action_"),
                CallbackQueryHandler(select_action, pattern="^back_start$"),
                CallbackQueryHandler(change_language, pattern="^change_language$"),
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
