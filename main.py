###############################################################################
# 🌟 main.py ─ Desafio Stripe → Pub/Sub (Cloud Functions gen 2) 🌟
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
#    • werkzeug.wrappers.Request / Response
#   O functions-framework entrega a requisição num formato bruto chamado
#   *WSGI environ* (um dicionário gigante cheio de chaves estranhas).
#
#   A biblioteca Werkzeug — que já vem instalada junto com
#   functions-framework — fornece dois “envelopes” simplificados:
#
#   1. Request  → transforma o environ num objeto com métodos fáceis:
#        • .get_data()   → corpo da requisição (bytes)
#        • .headers      → dicionário de headers
#        • .method       → GET, POST…
#        • .args         → query-string (?foo=1&bar=2)
#
#   2. Response → permite criar a resposta sem escrever cabeçalhos na mão:
#        • Response("texto", status=200, mimetype="text/plain")
#        • Response(json_str, mimetype="application/json")
#
#   Assim, conseguimos ler e responder HTTP de forma amigável **sem**
#   precisar instalar um framework inteiro como Flask ou FastAPI.  É
#   “canônico” porque ainda obedece ao padrão WSGI que o runtime espera.
# ────────────────────────────────────────────────────────────────────────────
import os, json, logging
import stripe
from werkzeug.wrappers import Request as WSGIRequest, Response as WSGIResponse


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
# 3.3  STRIPE_ENDPOINT_SECRET  →  assinatura do webhook (obrigatório)
#
# Estas variáveis NÃO DEVEM ser hard-codeadas.  Use Secret Manager ou
# --set-env-vars / --set-secrets no deploy.
# ────────────────────────────────────────────────────────────────────────────
#STRIPE_ENDPOINT_SECRET = os.getenv("STRIPE_ENDPOINT_SECRET", "whsec_mAUCESNM6KT8TKYTg2SqCAdhUAJr5hxy")
STRIPE_ENDPOINT_SECRET = os.getenv("STRIPE_ENDPOINT_SECRET", "whsec_8cb23418a763552f5fe32df43f87c7ed7335828b26956b719884c1636de2d0d0")
PROJECT_ID             = os.getenv("PROJECT_ID", "dev-stripe-webhoook-gfunc")
TOPIC_ID               = os.getenv("TOPIC_ID", "dev-stripe-webhoook-pubsub")

# Validação precoce: falha rápido se algo faltar
if not all([STRIPE_ENDPOINT_SECRET, PROJECT_ID, TOPIC_ID]):
    logger.critical(
        "⚠️  STRIPE_ENDPOINT_SECRET, PROJECT_ID ou TOPIC_ID não definidos!"
    )
    raise RuntimeError("Configuração incompleta.")

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


def publish(event_dict: dict) -> None:
    """Publica dicionário no Pub/Sub (mensagem = JSON bytes)."""
    data = json.dumps(event_dict).encode()
    publisher.publish(topic_path, data).result()  # .result() bloqueia até ACK
    logger.info("📤 Evento %s publicado no Pub/Sub", event_dict["id"])


# ────────────────────────────────────────────────────────────────────────────
# CAPÍTULO 6 — FUNÇÃO HTTP (TARGET) 🌐
# ---------------------------------------------------------------------------
# 6.1 Interface esperada: webhook(request)  ← o runtime chamará isso.
# 6.2 Health-check simples em GET (útil para monitoria).
# 6.3 Somente POST trata webhooks: valida assinatura, idempotência,
#     publica, devolve 200 ou erro adequado.
# 6.4 Qualquer erro interno gera 5xx → Stripe faz retry automático.
# ────────────────────────────────────────────────────────────────────────────
def webhook(request: WSGIRequest) -> WSGIResponse:
    # 6.1 Health-check
    if request.method == "GET":
        return WSGIResponse(json.dumps({"status": "live"}), mimetype="application/json")

    # 6.2 Só permitimos POST
    if request.method != "POST":
        return WSGIResponse("Método não permitido", status=405)

    # 6.3 Validação de assinatura
    raw_body  = request.get_data()
    signature = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(raw_body.decode(), signature, STRIPE_ENDPOINT_SECRET)
    except ValueError:
        return WSGIResponse("Invalid payload", status=400)
    except stripe.error.SignatureVerificationError:
        return WSGIResponse("Invalid signature", status=400)

    # 6.4 Idempotência + publicação
    eid = event["id"]
    if already_processed(eid):
        logger.info("🔄 Duplicate %s — ignorado", eid)
        return WSGIResponse(json.dumps({"status": "duplicate"}), mimetype="application/json")

    try:
        publish(event)
        mark_processed(eid)
        return WSGIResponse(json.dumps({"status": "ok"}), mimetype="application/json")
    except Exception as exc:
        logger.error("Falha Pub/Sub: %s", exc, exc_info=True)
        return WSGIResponse("Internal error", status=500)

# ────────────────────────────────────────────────────────────────────────────
# CAPÍTULO 7 — COMO USAR (PASSO A PASSO) 🛠️
# ---------------------------------------------------------------------------
# 7.1 Testar localmente
#     1) `pip install -r requirements.txt`
#     2) `python -m functions_framework --target=webhook --port 8080`
#     3) `stripe listen --forward-to localhost:8080`
#     4) `stripe trigger payment_intent.succeeded` (envia evento fictício)
#
# 7.2 Criar tópico Pub/Sub (uma vez):
#     gcloud pubsub topics create stripe-events
#
# 7.3 Guardar o secret no Secret Manager:
#     echo "whsec_..." | gcloud secrets create STRIPE_ENDPOINT_SECRET \
#       --data-file=- --replication-policy=automatic
#
# 7.4 Deploy:
#     gcloud functions deploy stripe-webhook \
#       --gen2 --runtime python310 --region us-central1 \
#       --trigger-http --allow-unauthenticated \
#       --entry-point webhook \
#       --memory 256Mi --timeout 60s --concurrency 10 \
#       --set-secrets STRIPE_ENDPOINT_SECRET=projects/$(gcloud config get-value project)/secrets/STRIPE_ENDPOINT_SECRET:latest \
#       --set-env-vars PROJECT_ID=$(gcloud config get-value project),TOPIC_ID=stripe-events,LOG_LEVEL=INFO
#       # acrescente USE_FIRESTORE_DEDUPE=true se quiser deduplicação persistente
#
# 7.5 Copiar a URL exibida (<…cloudfunctions.net/stripe-webhook>) e adicionar
#     na Stripe Dashboard  ➜ Developers ➜ Webhooks ➜ *Add endpoint*  (sufixo /).
#
# 7.6 Ver logs: Console → Operations → Logging → “stripe_webhook_func”.
#     Ver mensagens: Pub/Sub → tópicos → stripe-events → subscriptions.
#
# 7.7 Custos básicos:
#     • Cloud Functions escala a zero: US$ 0 quando ociosa.
#     • Pub/Sub: 1 Mi msgs ≈ US$ 0,40.
#     • Secret Manager: US$ 0,06 por segredo/mês.
#     • Firestore (se usado): centavos por Mi leis/gravações + TTL deletes.
###############################################################################
