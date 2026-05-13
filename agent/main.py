# agent/main.py - AgentKit Pergoland Chile con handoff humano
import os
import asyncio
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv
from agent.brain import generar_respuesta
from agent.memory import inicializar_db, guardar_mensaje, obtener_historial, obtener_historial_completo
from agent.providers import obtener_proveedor
from agent.crm import (
    enviar_lead_crm, enviar_lead_distribuidor_crm,
    extraer_datos_tag_madera, enviar_contacto_incompleto_crm
)
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


def es_lead_calificado(historial: list) -> bool:
    conversacion = " ".join([m["content"].lower() for m in historial])
    tiene_medidas = any(x in conversacion for x in ["x", "metro", "m2", "largo", "ancho", "medida"])
    tiene_comuna = any(x in conversacion for x in ["comuna", "santiago", "providencia", "las condes", "vitacura", "nunoa", "maipu", "rancagua", "valparaiso", "vina"])
    tiene_tipo = any(x in conversacion for x in ["terraza", "estacionamiento", "quincho", "piscina", "cochera"])
    return tiene_medidas and tiene_comuna and tiene_tipo


def tiene_tag_lead(historial: list) -> bool:
    for msg in reversed(historial):
        if msg["role"] == "assistant" and "[LEAD:" in msg["content"]:
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
    logger.info("Scheduler de recordatorios iniciado")

    yield

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(
    title="AgentKit - Matias de PERGOLAND CHILE SPA",
    version="1.3.0",
    lifespan=lifespan
)


@app.get("/")
async def health_check():
    return {"status": "ok", "service": "agentkit", "agente": "Matias", "negocio": "PERGOLAND CHILE SPA"}


@app.get("/conversations/{telefono}")
async def get_conversation(telefono: str):
    historial = await obtener_historial_completo(telefono)
    return {"messages": historial, "phone": telefono}


@app.post("/handoff/activar")
async def activar_handoff(request: Request):
    """
    Recibe trigger del CRM cuando cambia etapa.
    Body: { "telefono": "56912345678", "tipo": "cotizacion" | "visita" }
    """
    try:
        body = await request.json()
        telefono = body.get("telefono", "").strip()
        tipo = body.get("tipo", "").strip().lower()

        if not telefono or tipo not in ("cotizacion", "visita"):
            return {"status": "error", "message": "telefono y tipo (cotizacion/visita) requeridos"}

        # Normalizar teléfono
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
            if not msg.texto:
                continue

            texto = msg.texto.strip()

            # 1. Detectar "stop matias"
            if await es_comando_stop(texto):
                await pausar_contacto(msg.telefono)
                logger.info(f"Handoff activado para {msg.telefono} - Matias pausado")
                continue

            # 2. Detectar "start matias"
            if await es_comando_start(texto):
                await reanudar_contacto(msg.telefono)
                logger.info(f"Matías reanudado manualmente para {msg.telefono}")
                continue

            # 3. Ignorar mensajes propios
            if msg.es_propio:
                continue

            # 4. Si está pausado — no responder
            if await esta_pausado(msg.telefono):
                logger.info(f"Mensaje de {msg.telefono} ignorado - Matias pausado")
                continue

            # 5. Flujo normal
            logger.info(f"Mensaje de {msg.telefono}: {texto}")

            historial = await obtener_historial(msg.telefono)
            respuesta = await generar_respuesta(texto, historial)
            await guardar_mensaje(msg.telefono, "user", texto)
            await guardar_mensaje(msg.telefono, "assistant", respuesta)
            await proveedor.enviar_mensaje(msg.telefono, respuesta)

            historial_actualizado = await obtener_historial(msg.telefono)

            if tiene_tag_lead(historial_actualizado):
                await enviar_lead_crm(
                    msg.telefono,
                    msg.nombre if hasattr(msg, "nombre") else "",
                    historial_actualizado
                )
            elif extraer_datos_tag_madera(historial_actualizado):
                await enviar_lead_distribuidor_crm(msg.telefono, historial_actualizado)
            else:
                await enviar_contacto_incompleto_crm(
                    msg.telefono,
                    msg.nombre if hasattr(msg, "nombre") else "",
                    historial_actualizado
                )

            logger.info(f"Respuesta a {msg.telefono}: {respuesta}")

        return {"status": "ok"}

    except Exception as e:
        logger.error(f"Error en webhook: {e}")
        raise HTTPException(status_code=500, detail=str(e))
