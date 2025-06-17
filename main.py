###############################################################################
# 🌟 main.py ─ Desafio Stripe → Pub/Sub (Cloud Functions gen 2, Flask) 🌟
#
# Este arquivo foi dividido em capítulos e subcapítulos comentados para
# quem está aprendendo.  Siga os títulos → entenda cada etapa.  😉
###############################################################################


# ────────────────────────────────────────────────────────────────────────────
# CAPÍTULO 1 — IMPORTAÇÕES BÁSICAS
# ---------------------------------------------------------------------------
# 👉 Precisamos de:
#    • os / json  -> utilidades padrão do Python
#    • logging    -> registrar o que acontece (DEBUG, INFO, ERROR…)
#    • Flask      -> framework web WSGI (compatível nativo com Cloud Functions)
#    • stripe     -> SDK oficial p/ validar assinatura do webhook
# ────────────────────────────────────────────────────────────────────────────
import os
import json
import logging
from flask import Flask, request, jsonify
import stripe


# ────────────────────────────────────────────────────────────────────────────
# CAPÍTULO 2 — LOGGING ESTRUTURADO NO GOOGLE CLOUD 📜
# ---------------------------------------------------------------------------
# Por padrão, print() já vai p/ Cloud Logging, mas sem severidade correta.
# A linha abaixo intercepta o módulo logging e envia registros estruturados
# com campos "severity", "message", "timestamp", etc.
#
# • Se o projeto já tem Cloud Logging API habilitada (default), funciona direto.
# • Caso contrário, habilite no console: APIs & Services → Cloud Logging API.
# ────────────────────────────────────────────────────────────────────────────
import google.cloud.logging
google.cloud.logging.Client().setup_logging()  # INFO+ → Logs Explorer

# Cria logger nomeado (boa prática ao invés de usar root logger)
logger = logging.getLogger("stripe_webhook_func")
logger.setLevel(logging.INFO)  # exibirá INFO, WARNING, ERROR, CRITICAL


# ────────────────────────────────────────────────────────────────────────────
# CAPÍTULO 3 — CARREGANDO SEGREDOS & VARIÁVEIS DE AMBIENTE 🔐
# ---------------------------------------------------------------------------
# 3.1  STRIPE_ENDPOINT_SECRET  →  chave para verificar assinatura do webhook
# 3.2  PROJECT_ID & TOPIC_ID   →  onde publicaremos no Pub/Sub
# 3.3  (Opcional) STRIPE_API_KEY → caso precise consultar a API depois
#
# Estas variáveis NÃO DEVEM ser hard-codeadas.  Use Secret Manager ou
# --set-env-vars / --set-secrets no deploy.
# ────────────────────────────────────────────────────────────────────────────
STRIPE_ENDPOINT_SECRET = os.getenv("STRIPE_ENDPOINT_SECRET")
PROJECT_ID             = os.getenv("PROJECT_ID")
TOPIC_ID               = os.getenv("TOPIC_ID")

# Validação precoce: falha rápido se algo faltar
if not all([STRIPE_ENDPOINT_SECRET, PROJECT_ID, TOPIC_ID]):
    logger.critical(
        "⚠️  STRIPE_ENDPOINT_SECRET, PROJECT_ID ou TOPIC_ID não definidos!"
    )
    raise RuntimeError("Configuração incompleta.")

# A key da Stripe não é obrigatória para webhooks,
# mas mantemos para testes locais ou outras chamadas
stripe.api_key = os.getenv("STRIPE_API_KEY", "")


# ────────────────────────────────────────────────────────────────────────────
# CAPÍTULO 4 — CLIENTES GLOBAIS (REUSO) 🚀
# ---------------------------------------------------------------------------
# Criamos o Pub/Sub Publisher *uma vez*; as próximas invocações reutilizam
# a conexão.  Isso reduz cold-start e custo.
# ────────────────────────────────────────────────────────────────────────────
from google.cloud import pubsub_v1
publisher  = pubsub_v1.PublisherClient()
topic_path = publisher.topic_path(PROJECT_ID, TOPIC_ID)

# Idempotência opcional
FIRESTORE_DEDUPE = os.getenv("USE_FIRESTORE_DEDUPE", "false").lower() == "true"
if FIRESTORE_DEDUPE:
    from google.cloud import firestore
    fs_client = firestore.Client()
else:
    # Cache em memória: bom o suficiente para instância única por poucos min
    from cachetools import TTLCache
    _cache = TTLCache(maxsize=10_000, ttl=900)  # max 10k chaves, 15 min


# ────────────────────────────────────────────────────────────────────────────
# CAPÍTULO 5 — FUNÇÕES AUXILIARES 🔧
# ---------------------------------------------------------------------------
# 5.1 already_processed / mark_processed
#     → evitam duplicar trabalho se a Stripe reenviar o mesmo evento.
#
# 5.2 publish_pubsub
#     → serializa o evento em JSON e manda p/ tópico.
# ────────────────────────────────────────────────────────────────────────────
def already_processed(event_id: str) -> bool:
    """Verifica se o event_id já foi visto antes (idempotência)."""
    if FIRESTORE_DEDUPE:
        return fs_client.document(f"stripe_events/{event_id}").get().exists
    return event_id in _cache


def mark_processed(event_id: str):
    """Marca um event_id como processado."""
    if FIRESTORE_DEDUPE:
        fs_client.document(f"stripe_events/{event_id}").set({"processed": True})
    else:
        _cache[event_id] = True


def publish_pubsub(event_dict: dict) -> None:
    """Publica dicionário no Pub/Sub (mensagem = JSON bytes)."""
    data = json.dumps(event_dict).encode()
    publisher.publish(topic_path, data).result()  # .result() bloqueia até ACK
    logger.info("📤 Evento %s publicado no Pub/Sub", event_dict["id"])


# ────────────────────────────────────────────────────────────────────────────
# CAPÍTULO 6 — APLICATIVO FLASK (WSGI) 🍰
# ---------------------------------------------------------------------------
# Flask é WSGI nativo → Cloud Functions (runtime python) aceita diretamente.
# ────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)

# 6.1 Endpoint básico de health-check
@app.route("/", methods=["GET"])
def health():
    """🩺 Verifica se a função está viva."""
    return {"status": "live"}

# 6.2 Endpoint que a Stripe chama
@app.route("/webhook", methods=["POST"])
def stripe_webhook():
    """Processa eventos da Stripe."""
    raw_body  = request.data                # corpo cru (bytes)
    signature = request.headers.get("Stripe-Signature", "")

    # 6.2.1 Validação de assinatura
    try:
        event = stripe.Webhook.construct_event(
            raw_body.decode(), signature, STRIPE_ENDPOINT_SECRET
        )
    except ValueError:
        logger.warning("🛑 Payload JSON inválido")
        return "Invalid payload", 400
    except stripe.error.SignatureVerificationError:
        logger.warning("🛑 Assinatura Stripe inválida")
        return "Invalid signature", 400

    # 6.2.2 Checagem de duplicata
    eid = event["id"]
    if already_processed(eid):
        logger.info("🔄 Evento %s duplicado – ignorado", eid)
        return jsonify(status="duplicate"), 200

    # 6.2.3 Publicação no Pub/Sub
    try:
        publish_pubsub(event)
        mark_processed(eid)
        return jsonify(status="ok"), 200
    except Exception as exc:
        # Qualquer erro aqui resulta em 5xx → Stripe fará retry automático
        logger.error("Erro ao publicar: %s", exc, exc_info=True)
        return "Internal error", 500


# ────────────────────────────────────────────────────────────────────────────
# CAPÍTULO 7 — ENTRY-POINT PARA CLOUD FUNCTIONS 🌐
# ---------------------------------------------------------------------------
# Quando fazemos:
#    gcloud functions deploy stripe-webhook --entry-point app …
#
# O runtime procura um objeto WSGI chamado app.  NÃO precisamos
# escrever nenhuma função wrapper extra.
# ────────────────────────────────────────────────────────────────────────────

###############################################################################
# DICAS DE DEPLOY
# -----------------------------------------------------------------------------
# • Deploy gen 2, runtime python310, sem Docker:
#
# gcloud functions deploy stripe-webhook \
#   --gen2 --runtime python310 --region us-central1 \
#   --trigger-http --allow-unauthenticated \
#   --entry-point app \
#   --memory 256Mi --timeout 60s --concurrency 10 \
#   --set-secrets STRIPE_ENDPOINT_SECRET=projects/$PRJ/secrets/STRIPE_ENDPOINT_SECRET:latest \
#   --set-env-vars PROJECT_ID=$PROJECT_ID,TOPIC_ID=$TOPIC_ID,USE_FIRESTORE_DEDUPE=true
#
# • Teste local (necessário functions-framework no requirements.txt):
#   python -m functions_framework --target=app
#   stripe listen --forward-to localhost:8080/webhook
###############################################################################
