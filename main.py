import requests
import json
import os
import time
import unidecode
import heapq
import random

import notion_tools

# ================= CONFIGURATION =================
VERBOSE = True  # Afficher les logs détaillés
MAX_TOOL_ITERATIONS = 5  # Limite de sécurité pour la boucle de tool-calling


# ================= CHARGEMENT CONFIG =================
def load_file_if_needed(value):
    """Charge un fichier si la valeur commence par 'file:'"""
    if isinstance(value, str) and value.startswith("file:"):
        filename = value[5:]
        with open(filename, "r", encoding="utf-8") as f:
            return f.read().splitlines()
    return value


def load_config():
    """Charge et valide la configuration"""
    with open("config.json", "r", encoding="utf-8") as f:
        cfg = json.load(f)

    # Chargement des valeurs
    bot_token = load_file_if_needed(cfg["BOT_TOKEN"])
    if isinstance(bot_token, list):
        bot_token = bot_token[0]

    system_prompt = load_file_if_needed(cfg["SYSTEM_PROMPT"])
    summary_prompt = load_file_if_needed(cfg["SUMMARY_PROMPT"])

    return {
        "BOT_TOKEN": bot_token,
        "SYSTEM_PROMPT": system_prompt,
        "SUMMARY_PROMPT": summary_prompt,
        "ADMIN_ID": cfg["ADMIN_ID"],
        "DATA_DIR": cfg["DATA_DIR"],
        "LM_STUDIO_URL": cfg["LM_STUDIO_URL"],
        "MODEL_NAME": cfg["MODEL_NAME"],
        "MAX_CONTEXT": 2 * cfg["MAX_CONTEXT"],  # Users usually perceive context as number of messages they've sent
        "ERROR_LIMIT": cfg["ERROR_LIMIT"],
        "POLL_INTERVAL": cfg["POLL_INTERVAL"],
        "SUMMARY_CONTEXT_LINES": cfg.get("SUMMARY_CONTEXT_LINES", 2),
        "LLM_TEMPERATURE": cfg.get("LLM_TEMPERATURE", 0.7),
        "LLM_MAX_TOKENS": cfg.get("LLM_MAX_TOKENS", 1000),
        "MAX_USER_MSG_LENGTH": cfg.get("MAX_USER_MSG_LENGTH", 2000),
        "MAX_LLM_MSG_LENGTH": cfg.get("MAX_LLM_MSG_LENGTH", 4000),
        "SPAM_WINDOW": cfg.get("SPAM_WINDOW", 10),
        "SPAM_MAX_MESSAGES": cfg.get("SPAM_MAX_MESSAGES", 3),
        "SPAM_TIMEOUT": cfg.get("SPAM_TIMEOUT", 30),
        # Notion tool-calling (optional - feature stays disabled if no key is set)
        "NOTION_API_KEY": os.environ.get("NOTION_API_KEY") or cfg.get("NOTION_API_KEY"),
        "NOTION_DEFAULT_PARENT_ID": cfg.get("NOTION_DEFAULT_PARENT_ID"),
        "NOTION_DEFAULT_PARENT_TYPE": cfg.get("NOTION_DEFAULT_PARENT_TYPE", "database_id"),
        "TOOLS_ENABLED": cfg.get("TOOLS_ENABLED", True),
    }


CONFIG = load_config()
os.makedirs(CONFIG["DATA_DIR"], exist_ok=True)

notion_tools.configure(
    api_key=CONFIG["NOTION_API_KEY"],
    default_parent_id=CONFIG["NOTION_DEFAULT_PARENT_ID"],
    default_parent_type=CONFIG["NOTION_DEFAULT_PARENT_TYPE"],
)


# ================= ÉTAT GLOBAL =================
class BotState:
    """Gestion de l'état du bot"""

    def __init__(self):
        self.suspended = False
        self.llm_error_count = 0
        self.user_activity = {}  # user_id -> [timestamps]
        self.user_muted_until = {}  # user_id -> timestamp
        self.request_queue = []  # File d'attente prioritaire
        self.queue_sequence = 0  # Séquence pour FIFO sur priorité égale


state = BotState()


# ================= LOGGING =================
def vlog(msg):
    """Log verbose si activé"""
    if VERBOSE:
        print(f"[{time.strftime('%H:%M:%S')}] {msg}")


# ================= GESTION FICHIERS =================
def chat_file(chat_id):
    """Retourne le chemin du fichier de chat"""
    return os.path.join(CONFIG["DATA_DIR"], f"chat_{chat_id}.json")


def load_chat(chat_id):
    """Charge l'historique d'un chat"""
    path = chat_file(chat_id)
    if not os.path.exists(path):
        return {"chat_id": chat_id, "offset": 0, "history": []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_chat(chat):
    """Sauvegarde l'historique d'un chat"""
    with open(chat_file(chat["chat_id"]), "w", encoding="utf-8") as f:
        json.dump(chat, f, ensure_ascii=False, indent=2)


# ================= NORMALISATION TEXTE =================
def normalize_text(text):
    """Normalise les espaces spéciaux"""
    return text.replace("\u00a0", " ").replace("\u202f", " ").replace("\u200b", "")


def normalize_ascii(text):
    """Convertit en ASCII et normalise"""
    return unidecode.unidecode(text).replace("\u200b", "")


# ================= TELEGRAM API =================
def send_telegram(chat_id, text, reply_to_message_id=None):
    """Envoie un message Telegram"""
    try:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
            "disable_web_page_preview": True
        }

        if reply_to_message_id:
            payload["reply_to_message_id"] = reply_to_message_id

        url = f"https://api.telegram.org/bot{CONFIG['BOT_TOKEN']}/sendMessage"

        resp = requests.post(
            url,
            json=payload,
            timeout=10
        )

        print(f"📤 Telegram 1차 응답: {resp.status_code}")
        print(f"📤 Telegram 내용: {resp.text}")

        if resp.status_code != 200:
            print("⚠️ Markdown 없이 다시 전송합니다.")

            payload.pop("parse_mode", None)

            resp2 = requests.post(
                url,
                json=payload,
                timeout=10
            )

            print(f"📤 Telegram 2차 응답: {resp2.status_code}")
            print(f"📤 Telegram 내용: {resp2.text}")

    except Exception as e:
        print(f"❌ Telegram 전송 오류: {e}")


def send_telegram_limited(chat_id, text, reply_to_message_id=None):
    """Envoie un message avec limite de taille"""
    length = len(text)
    max_length = CONFIG["MAX_LLM_MSG_LENGTH"]

    if length <= max_length:
        send_telegram(chat_id, text, reply_to_message_id)
        return True

    # Si trop long, découpe en deux
    half = length // 2
    if half > max_length:
        send_telegram(chat_id, "⚠️ Le message généré est trop long pour être envoyé.", reply_to_message_id)
        return False

    send_telegram(chat_id, text[:half], reply_to_message_id)
    time.sleep(1)  # Éviter les rate limits Telegram
    send_telegram(chat_id, text[half:]) # Should 2nd message reply to the first half?
    return True


def get_updates(offset=None):
    """Récupère les mises à jour Telegram"""
    params = {"timeout": 30}
    if offset is not None:
        params["offset"] = offset + 1

    return requests.get(
        f"https://api.telegram.org/bot{CONFIG['BOT_TOKEN']}/getUpdates",
        params=params,
        timeout=35
    ).json()


# ================= ANTI-SPAM =================
def purge_queue_for_user(user_id):
    """Retire toutes les requêtes d'un utilisateur de la file d'attente"""
    original_size = len(state.request_queue)
    # Filtrer la queue pour exclure les messages de cet utilisateur
    filtered_queue = []

    for priority, seq, update in state.request_queue:
        msg = update.get("message", {})
        msg_user_id = msg.get("from", {}).get("id")

        if msg_user_id != user_id:
            filtered_queue.append((priority, seq, update))

    # Reconstruire la heap
    state.request_queue = []
    for item in filtered_queue:
        heapq.heappush(state.request_queue, item)

    purged_count = original_size - len(state.request_queue)
    if purged_count > 0:
        vlog(f"🗑️  {purged_count} requête(s) retirée(s) de la queue pour user {user_id}")


def check_spam(user_id, chat_id):
    """
    Vérifie si un utilisateur spam et applique un mute si nécessaire.
    Retourne True si l'utilisateur est autorisé, False s'il est muted.
    """
    now = time.time()

    # Vérifier si l'utilisateur est déjà muted
    if user_id in state.user_muted_until:
        if now < state.user_muted_until[user_id]:
            # Calculer le temps restant
            time_remaining = state.user_muted_until[user_id] - now

            # Augmenter le ban de 10%
            extension = time_remaining * 0.10
            state.user_muted_until[user_id] += extension

            new_remaining = state.user_muted_until[user_id] - now
            vlog(
                f"⛔ User {user_id} encore muted - Ban prolongé de {extension:.1f}s (nouveau total: {new_remaining:.1f}s)")

            # Informer l'utilisateur seulement 10% du temps
            if random.randint(0, 99) < 10:
                send_telegram(chat_id,
                              f"⏳ Ban prolongé pour message pendant le mute. Temps restant: {new_remaining:.1f}s")
            return False
        else:
            # Le mute a expiré
            del state.user_muted_until[user_id]
            vlog(f"✅ Mute expiré pour user {user_id}")

    # Nettoyer et mettre à jour l'activité
    activity = state.user_activity.setdefault(user_id, [])
    activity[:] = [t for t in activity if now - t <= CONFIG["SPAM_WINDOW"]]
    activity.append(now)

    # Vérifier le spam
    if len(activity) > CONFIG["SPAM_MAX_MESSAGES"]:
        state.user_muted_until[user_id] = now + CONFIG["SPAM_TIMEOUT"]
        send_telegram(chat_id, f"⏳ Trop de messages en peu de temps. Pause de {CONFIG['SPAM_TIMEOUT']}s.")
        vlog(f"⚠️ Utilisateur {user_id} muted pour {CONFIG['SPAM_TIMEOUT']}s (spam détecté)")

        # Retirer toutes ses requêtes de la queue
        purge_queue_for_user(user_id)

        return False

    return True


def calculate_priority(user_id):
    """
    Calcule la priorité d'une requête.
    Plus le score est élevé, moins la priorité est haute.
    """
    activity_count = len(state.user_activity.get(user_id, []))
    # Priorité basée sur l'activité récente + randomisation pour équité
    return 1.0 + (activity_count * 0.5) + random.random()


# ================= COMPRESSION HISTORIQUE =================
def summarize_block(history, start_idx, end_idx):
    """Résume un bloc de l'historique"""
    vlog(f"🧠 Résumé de history[{start_idx}:{end_idx}] ({end_idx - start_idx} messages)")

    # Contexte avant et après pour le modèle
    head_context = history[max(0, start_idx - CONFIG["SUMMARY_CONTEXT_LINES"]):start_idx]
    tail_context = history[end_idx:end_idx + CONFIG["SUMMARY_CONTEXT_LINES"]]
    block = history[start_idx:end_idx]

    messages = [{"role": "system", "content": "\n- ".join(CONFIG["SUMMARY_PROMPT"])}]
    messages += head_context
    # Bloc à résumer (on garde les rôles tels quels)
    for msg in block:
        if "role" in msg and "content" in msg:
            messages.append({
                "role": msg["role"],
                "content": msg["content"]
            })
    messages += tail_context

    try:
        r = requests.post(
            CONFIG["LM_STUDIO_URL"],
            json={
                "model": CONFIG["MODEL_NAME"],
                "messages": messages,
                "temperature": CONFIG["LLM_TEMPERATURE"],
                "max_tokens": 1024
            },
            timeout=60
        )
        r.raise_for_status()
        summary = r.json()["choices"][0]["message"]["content"]
        return normalize_ascii(summary)
    except Exception as e:
        print(f"❌ Erreur lors du résumé: {e}")
        return "[RÉSUMÉ ÉCHOUÉ]"


def compress_history(history):
    """Compresse l'historique si trop long"""
    if len(history) <= CONFIG["MAX_CONTEXT"]:
        return history

    vlog(f"📦 Compression historique: {len(history)} → {CONFIG['MAX_CONTEXT']} messages")

    # Garder le début et la fin, résumer le milieu
    head_count = max(1, CONFIG["MAX_CONTEXT"] // 4)
    tail_count = max(1, CONFIG["MAX_CONTEXT"] // 4)
    middle_count = CONFIG["MAX_CONTEXT"] - head_count - tail_count

    head = history[:head_count]
    tail = history[-tail_count:]
    middle = history[head_count:-tail_count]

    compressed = []
    idx = 0

    while middle:
        chunk_size = min(middle_count, len(middle))
        summary_text = summarize_block(history, head_count + idx, head_count + idx + chunk_size)
        compressed.append({"role": "user", "content": f"[CONTEXT SUMMARY. derived from pruned messages]{summary_text}[/CONTEXT SUMMARY]"})
        idx += chunk_size
        middle = middle[chunk_size:]

    return head + compressed + tail


# ================= APPEL LLM =================

def request_llm_completion(messages, tools=None):
    """Envoie une requête de complétion à LM Studio, avec tools optionnels."""
    payload = {
        "model": CONFIG["MODEL_NAME"],
        "messages": messages,
        "temperature": CONFIG["LLM_TEMPERATURE"],
        "max_tokens": CONFIG["LLM_MAX_TOKENS"],
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    start = time.time()
    r = requests.post(
        CONFIG["LM_STUDIO_URL"],
        json=payload,
        timeout=60
    )
    r.raise_for_status()
    duration = time.time() - start
    vlog(f"⏱️  LLM répondu en {duration:.2f}s")

    raw = r.json()
    vlog(f"🔍 RAW RESPONSE: {raw}")

    return raw["choices"][0].get("message", {})


def call_llm(history, user_prompt, user_id, username, chat_id, chat_name):
    """Appelle le modèle LLM avec une boucle de tool-calling (Notion)."""

    formatted_prompt = (
        f"[USER_PROMPT USER=({user_id}{', ' + username if username else ''}) "
        f"CHAT=({chat_id}, {chat_name})]\n"
        f"{user_prompt}\n"
        f"/no_think\n"
        f"[/USER_PROMPT]"
    )

    messages = [{"role": "system", "content": "\n".join(CONFIG["SYSTEM_PROMPT"])}]
    messages += history
    messages.append({"role": "user", "content": formatted_prompt})

    # Si LM Studio expose déjà les tools MCP de Notion nativement, il pourra
    # les résoudre lui-même côté serveur. Sinon, on exécute le fallback Python
    # ci-dessous dès qu'un tool_call est détecté dans la réponse.
    tools = notion_tools.TOOL_DEFINITIONS if CONFIG["TOOLS_ENABLED"] else None

    for iteration in range(1, MAX_TOOL_ITERATIONS + 1):
        try:
            message = request_llm_completion(messages, tools=tools)
        except requests.exceptions.Timeout:
            print("⏱️ LM Studio 응답 시간 초과 (timeout).")
            return "죄송해요. 응답 시간이 초과됐어요. 잠시 후 다시 시도해 주세요."

        tool_calls = message.get("tool_calls")

        if tool_calls:
            vlog(f"🛠️  모델이 {len(tool_calls)}개의 tool 호출을 요청함 (반복 {iteration}/{MAX_TOOL_ITERATIONS})")
            messages.append(message)

            for call in tool_calls:
                fn = call.get("function", {})
                name = fn.get("name")
                raw_args = fn.get("arguments") or "{}"

                try:
                    arguments = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except json.JSONDecodeError:
                    print(f"⚠️ Tool arguments JSON invalide pour {name}: {raw_args}")
                    arguments = {}

                vlog(f"🔧 Tool 선택: {name} | 인자: {arguments}")
                result = notion_tools.execute_tool(name, arguments)
                vlog(f"📦 Tool 결과 ({name}): {result}")

                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "name": name,
                    "content": notion_tools.tool_result_to_content(result),
                })

            continue  # Renvoyer les résultats des tools au modèle

        content = (message.get("content") or "").strip()
        reasoning = (message.get("reasoning_content") or message.get("reasoning") or "").strip()

        if content:
            vlog(f"✅ 최종 모델 응답: {content[:200]}")
            return content

        if reasoning:
            print("⚠️ LLM이 reasoning만 반환하고 최종 답변을 만들지 않았습니다.")
        else:
            print("⚠️ LLM이 빈 응답을 반환했습니다.")

        return "죄송해요. 응답을 만들지 못했어요. 다시 시도해 주세요."

    print("⚠️ Tool-calling 루프가 최대 반복 횟수에 도달했습니다.")
    return "죄송해요. 요청을 처리하는 데 문제가 발생했어요. 다시 시도해 주세요."


# ================= FILE D'ATTENTE =================
def enqueue_request(update, user_id):
    """Ajoute une requête à la file d'attente avec priorité"""
    state.queue_sequence += 1
    priority = calculate_priority(user_id)
    heapq.heappush(state.request_queue, (priority, state.queue_sequence, update))
    vlog(f"📥 Requête en queue - User: {user_id}, Priorité: {priority:.2f}, Taille queue: {len(state.request_queue)}")


def process_request(update):
    """Traite une requête de la file d'attente"""
    msg = update["message"]
    chat_id = msg["chat"]["id"]
    message_id = msg["message_id"]
    text = normalize_text(msg.get("text", ""))

    from_data = msg.get("from", {})
    user_id = from_data.get("id")
    username = from_data.get("username")
    from_user = from_data.get("first_name", "Utilisateur")
    chat_name = msg["chat"].get("title") or from_user

    vlog(f"⚙️  Traitement requête de {from_user} (user_id: {user_id})")

    try:
        # Charger et compresser l'historique
        chat = load_chat(chat_id)
        chat["history"] = compress_history(chat["history"])

        # Ajouter le message utilisateur
        chat["history"].append({"role": "user", "content": text})

        # Appeler le LLM
        reply = call_llm(chat["history"], text, user_id, username, chat_id, chat_name)
        reply = normalize_text(reply)

        # Sauvegarder et envoyer
        chat["history"].append({"role": "assistant", "content": reply})
        send_telegram_limited(chat_id, reply, reply_to_message_id=message_id)

        chat["offset"] = update["update_id"]
        save_chat(chat)

        # Réinitialiser le compteur d'erreurs
        state.llm_error_count = 0
        vlog(f"✅ Requête traitée avec succès pour {from_user}")

    except Exception as e:
        print(f"❌ Erreur lors du traitement: {e}")
        state.llm_error_count += 1

        if state.llm_error_count >= CONFIG["ERROR_LIMIT"]:
            state.suspended = True
            send_telegram(
                CONFIG["ADMIN_ID"],
                f"⚠️ Bot suspendu après {CONFIG['ERROR_LIMIT']} erreurs consécutives.\n"
                f"Dernière erreur: {str(e)}"
            )
            vlog(f"🛑 Bot suspendu après {CONFIG['ERROR_LIMIT']} erreurs")


# ================= INITIALISATION =================
def initialize_offset():
    """Initialise l'offset global depuis les fichiers sauvegardés"""
    global_offset = 0
    data_dir = CONFIG["DATA_DIR"]

    if not os.path.exists(data_dir):
        return global_offset

    for filename in os.listdir(data_dir):
        if filename.startswith("chat_") and filename.endswith(".json"):
            try:
                with open(os.path.join(data_dir, filename), "r", encoding="utf-8") as f:
                    chat_data = json.load(f)
                    global_offset = max(global_offset, chat_data.get("offset", 0))
            except Exception as e:
                print(f"⚠️  Erreur lecture {filename}: {e}")

    return global_offset


# ================= BOUCLE PRINCIPALE =================
def main():
    """Boucle principale du bot"""
    print("=" * 60)
    print("🤖 Bot Telegram + LM Studio avec Anti-Spam")
    print("=" * 60)
    print(f"📁 Répertoire données: {CONFIG['DATA_DIR']}")
    print(f"🌐 LM Studio: {CONFIG['LM_STUDIO_URL']}")
    print(f"🧠 Modèle: {CONFIG['MODEL_NAME']}")
    print(
        f"🛡️  Anti-spam: {CONFIG['SPAM_MAX_MESSAGES']} msg / {CONFIG['SPAM_WINDOW']}s → mute {CONFIG['SPAM_TIMEOUT']}s")
    print("=" * 60)

    global_offset = initialize_offset()
    vlog(f"🔄 Offset initial: {global_offset}")

    while True:
        # Vérifier si le bot est suspendu
        if state.suspended:
            vlog("⏸️  Bot suspendu, attente...")
            time.sleep(30)
            continue

        # Récupérer les mises à jour
        try:
            updates = get_updates(global_offset)
        except Exception as e:
            print(f"❌ Erreur getUpdates: {e}")
            time.sleep(CONFIG["POLL_INTERVAL"])
            continue

        # Traiter chaque mise à jour
        for update in updates.get("result", []):
            global_offset = update["update_id"]

            msg = update.get("message")
            if not msg:
                continue

            chat_id = msg["chat"]["id"]
            message_id = msg["message_id"]
            text = normalize_text(msg.get("text", ""))

            from_data = msg.get("from", {})
            user_id = from_data.get("id")
            username = from_data.get("username")
            from_user = from_data.get("first_name", "Utilisateur")

            # Ignorer les messages vides
            if not text.strip():
                continue

            # Vérifier la longueur du message
            if len(text) > CONFIG["MAX_USER_MSG_LENGTH"]:
                send_telegram(
                    chat_id,
                    f"⚠️ Message trop long ({len(text)} > {CONFIG['MAX_USER_MSG_LENGTH']} caractères)",
                    reply_to_message_id=message_id
                )
                continue

            print(f"💬 [{from_user}] {text[:60]}{'...' if len(text) > 60 else ''}")

            # Vérifier le spam
            if not check_spam(user_id, chat_id):
                # Message ignoré, mais on le marque comme lu
                vlog(f"📭 Message ignoré (user muted) mais marqué comme lu")
                continue

            # Ajouter à la file d'attente
            enqueue_request(update, user_id)

        # Traiter une requête de la file d'attente
        if state.request_queue and not state.suspended:
            _, _, update = heapq.heappop(state.request_queue)
            process_request(update)

        # Pause avant la prochaine itération
        time.sleep(CONFIG["POLL_INTERVAL"])


# ================= POINT D'ENTRÉE =================
if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n🛑 Arrêt du bot...")
    except Exception as e:
        print(f"❌ Erreur fatale: {e}")
        raise
