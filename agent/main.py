# agent/main.py - AgentKit Pergoland Argentina (Gian) con handoff humano
import os
import re
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv
from agent.brain import generar_respuesta
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial, obtener_historial_completo
from agent.providers import obtener_proveedor
from agent.flujo import despachar_respuesta, preparar_para_cliente
from agent.handoff import (
    inicializar_handoff_db, pausar_contacto, reanudar_contacto,
    activar_timer, esta_pausado, es_comando_stop, es_comando_start,
    scheduler_recordatorios
)

load_dotenv()

ENVIRONMENT = os.getenv("ENVIRONMENT", "development")
PORT = int(os.getenv("PORT", 8080))
log_level = logging.DEBUG if ENVIRONMENT == "development" else logging.INFO
logging.basicConfig(level=log_level)
logger = logging.getLogger("agentkit")

proveedor = obtener_proveedor()

# Cache de mensajes procesados para evitar duplicados de Whapi
mensajes_procesados: set[str] = set()

# Mensajes automáticos de WhatsApp Business (Meta) que llegan como from_me=True
# pero NO fueron escritos por una persona — no deben pausar a Gian
MENSAJES_AUTOMATICOS_META = [
    "Gracias por contactarte con Pergoland Chile",
    "Gracias por contactarte con Pergoland Argentina",
]

# Primer mensaje que envía GHL (vía Whapi) cuando el cliente completa el formulario:
# "Hola [nombre], soy Gian de Pergoland. Te escribo por tu interés..."
# Se guarda como turno de Gian y NO cuenta como intervención humana.
PATRON_MENSAJE_GHL = re.compile(r"^\s*hola\b.*?soy gian de pergoland", re.IGNORECASE | re.DOTALL)


def es_mensaje_automatico_meta(texto: str) -> bool:
    if not texto:
        return False
    return any(texto.strip().startswith(p) for p in MENSAJES_AUTOMATICOS_META)


def es_mensaje_inicial_ghl(texto: str) -> bool:
    return bool(texto) and bool(PATRON_MENSAJE_GHL.match(texto))


def _normalizar_espacios(texto: str) -> str:
    return re.sub(r"\s+", " ", texto or "").strip()


async def es_eco_de_gian(telefono: str, texto: str) -> bool:
    """
    True si el mensaje saliente es el eco de algo que Gian ya envió.
    Se compara contra la versión que efectivamente recibió el cliente
    (sin tags internos ni emojis) de los últimos mensajes de Gian.
    """
    if not texto:
        return False
    objetivo = _normalizar_espacios(texto)
    for m in reversed(await obtener_historial(telefono, limite=10)):
        if m["role"] != "assistant" or m["content"].startswith("[INTERVENCION_HUMANA]"):
            continue
        if _normalizar_espacios(preparar_para_cliente(m["content"])) == objetivo:
            return True
    return False


@asynccontextmanager
async def lifespan(app: FastAPI):
    await inicializar_db()
    await inicializar_handoff_db()
    logger.info("Base de datos inicializada")
    logger.info(f"Servidor AgentKit corriendo en puerto {PORT}")
    logger.info(f"Proveedor de WhatsApp: {proveedor.__class__.__name__}")

    task = asyncio.create_task(scheduler_recordatorios(proveedor))
    logger.info("Scheduler de recordatorios y seguimientos iniciado")

    yield

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="AgentKit - Gian de PERGOLAND ARGENTINA",
    version="1.7.0",
    lifespan=lifespan
)


@app.get("/")
async def health_check():
    return {"status": "ok", "service": "agentkit", "agente": "Gian", "negocio": "PERGOLAND ARGENTINA"}


@app.get("/conversations/{telefono}")
async def get_conversation(telefono: str):
    historial = await obtener_historial_completo(telefono)
    return {"messages": historial, "phone": telefono}


@app.post("/handoff/activar")
async def activar_handoff(request: Request):
    try:
        body = await request.json()
        telefono = body.get("telefono", "").strip()
        tipo = body.get("tipo", "").strip().lower()

        if not telefono or tipo not in ("cotizacion", "visita"):
            return {"status": "error", "message": "telefono y tipo (cotizacion/visita) requeridos"}

        if not telefono.endswith("@s.whatsapp.net"):
            telefono = f"{telefono}@s.whatsapp.net"

        await activar_timer(telefono, tipo)
        logger.info(f"Timer {tipo} activado para {telefono} desde CRM")
        return {"status": "ok", "telefono": telefono, "tipo": tipo}

    except Exception as e:
        logger.error(f"Error activando handoff: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/webhook")
async def webhook_verificacion(request: Request):
    from starlette.responses import Response
    resultado = await proveedor.validar_webhook(request)
    if resultado is None:
        return {"status": "ok"}
    if isinstance(resultado, Response):
        return resultado
    return PlainTextResponse(str(resultado))


@app.post("/webhook")
async def webhook_handler(request: Request):
    try:
        mensajes = await proveedor.parsear_webhook(request)

        for msg in mensajes:
            # Ignorar mensajes duplicados (Whapi envía el mismo webhook varias veces)
            if msg.mensaje_id and msg.mensaje_id in mensajes_procesados:
                logger.info(f"Mensaje duplicado ignorado: {msg.mensaje_id}")
                continue
            if msg.mensaje_id:
                mensajes_procesados.add(msg.mensaje_id)

            # 1. Mensajes salientes del número de Pergoland (from_me=True):
            #    a) "stop gian" / "start gian" escritos por Martín.
            #    b) Eco de un mensaje que Gian ya envió -> se ignora.
            #    c) Mensaje automático de WhatsApp Business (Meta) -> se ignora.
            #    d) Primer mensaje de GHL ("Hola ..., soy Gian de Pergoland") ->
            #       se guarda como turno de Gian, sin pausar.
            #    e) Cualquier otro (texto o archivo) -> intervención humana + pausa.
            if msg.es_propio:
                texto_humano = msg.texto.strip() if msg.texto else ""

                if texto_humano and await es_comando_stop(texto_humano):
                    await pausar_contacto(msg.telefono)
                    logger.info(f"Gian pausado para {msg.telefono} (orden manual)")
                    continue
                if texto_humano and await es_comando_start(texto_humano):
                    await reanudar_contacto(msg.telefono)
                    logger.info(f"Gian reanudado para {msg.telefono} (orden manual)")
                    continue

                if await es_eco_de_gian(msg.telefono, texto_humano):
                    logger.info(f"Mensaje ignorado (eco de Gian) para {msg.telefono}")
                    continue
                if es_mensaje_automatico_meta(texto_humano):
                    logger.info(f"Mensaje ignorado (automático WhatsApp Business) para {msg.telefono}")
                    continue
                if es_mensaje_inicial_ghl(texto_humano):
                    await guardar_mensaje(msg.telefono, "assistant", texto_humano)
                    logger.info(f"Mensaje inicial de GHL guardado como turno de Gian para {msg.telefono}")
                    continue

                # Intervención humana real: registrar Y pausar
                contenido_log = texto_humano if texto_humano else "Se envió un archivo"
                await guardar_mensaje(msg.telefono, "assistant", f"[INTERVENCION_HUMANA] {contenido_log}")
                await pausar_contacto(msg.telefono)
                logger.info(f"Intervención humana registrada y Gian pausado para {msg.telefono}: {contenido_log}")
                continue

            if not msg.texto:
                continue

            texto = msg.texto.strip()

            # 2. "stop gian" / "start gian" escritos desde el chat del cliente (caso raro)
            if await es_comando_stop(texto):
                await pausar_contacto(msg.telefono)
                logger.info(f"Gian pausado para {msg.telefono}")
                continue
            if await es_comando_start(texto):
                await reanudar_contacto(msg.telefono)
                logger.info(f"Gian reanudado manualmente para {msg.telefono}")
                continue

            # 3. Si está pausado (STOP, intervención humana o cierre ya enviado): no responder
            if await esta_pausado(msg.telefono):
                logger.info(f"Mensaje de {msg.telefono} ignorado - Gian pausado")
                continue

            # 4. Flujo normal
            logger.info(f"Mensaje de {msg.telefono}: {texto}")

            historial = await obtener_historial(msg.telefono)
            respuesta = await generar_respuesta(texto, historial)
            await guardar_mensaje(msg.telefono, "user", texto)
            await guardar_mensaje(msg.telefono, "assistant", respuesta)

            # Envía limpio (sin tags ni emojis), avisa al CRM y pausa si es cierre
            enviado = await despachar_respuesta(
                proveedor, msg.telefono,
                msg.nombre if hasattr(msg, "nombre") else "",
                respuesta,
            )
            logger.info(f"Respuesta a {msg.telefono}: {enviado}")

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))
