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
    esta_pausado, es_comando_stop, scheduler_recordatorios
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
    """Verifica si Matías ya generó el tag [LEAD:...] en la conversación."""
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
    version="1.2.0",
    lifespan=lifespan
)


@app.get("/")
async def health_check():
    return {"status": "ok", "service": "agentkit", "agente": "Matias", "negocio": "PERGOLAND CHILE SPA"}


@app.get("/conversations/{telefono}")
async def get_conversation(telefono: str):
    historial = await obtener_historial_completo(telefono)
    return {"messages": historial, "phone": telefono}


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

            # 1. Detectar "stop matias" desde cualquier numero
            if await es_comando_stop(texto):
                await pausar_contacto(msg.telefono)
                logger.info(f"Handoff activado para {msg.telefono} - Matias pausado")
                continue

            # 2. Ignorar mensajes propios que no son stop matias
            if msg.es_propio:
                continue

            # 3. Si esta pausado - no responder (Gabriel esta atendiendo)
            if await esta_pausado(msg.telefono):
                logger.info(f"Mensaje de {msg.telefono} ignorado - Matias pausado (Gabriel atendiendo)")
                continue

            # 4. Flujo normal - Matias responde
            logger.info(f"Mensaje de {msg.telefono}: {texto}")

            historial = await obtener_historial(msg.telefono)
            respuesta = await generar_respuesta(texto, historial)
            await guardar_mensaje(msg.telefono, "user", texto)
            await guardar_mensaje(msg.telefono, "assistant", respuesta)
            await proveedor.enviar_mensaje(msg.telefono, respuesta)

            historial_actualizado = await obtener_historial(msg.telefono)

            # 5. Verificar si es lead calificado (solo si tiene el tag LEAD)
            if tiene_tag_lead(historial_actualizado):
                await enviar_lead_crm(
                    msg.telefono,
                    msg.nombre if hasattr(msg, "nombre") else "",
                    historial_actualizado
                )
            elif extraer_datos_tag_madera(historial_actualizado):
                await enviar_lead_distribuidor_crm(msg.telefono, historial_actualizado)
            else:
                # 6. Guardar contacto incompleto para remarketing
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
