# agent/main.py - AgentKit Pergoland Argentina con handoff humano
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

# Cache de mensajes procesados para evitar duplicados de Whapi
mensajes_procesados: set[str] = set()


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
    title="AgentKit - Gian de PERGOLAND ARGENTINA",
    version="1.5.0",
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

            # 1. Mensajes propios (Martín escribiendo manualmente desde Whapi)
            #    Se registran como intervención humana en el historial — Gian sigue activo,
            #    pero ya no va a repreguntar datos que Martín ya resolvió manualmente.
            if msg.es_propio:
                texto_martin = msg.texto.strip() if msg.texto else ""

                # "stop gian" / "start gian" escritos por Martín siguen funcionando igual
                if texto_martin and await es_comando_stop(texto_martin):
                    await pausar_contacto(msg.telefono)
                    logger.info(f"Handoff activado para {msg.telefono} - Gian pausado (orden de Martín)")
                    continue
                if texto_martin and await es_comando_start(texto_martin):
                    await reanudar_contacto(msg.telefono)
                    logger.info(f"Gian reanudado para {msg.telefono} (orden de Martín)")
                    continue

                contenido_log = texto_martin if texto_martin else "Se envió un archivo"
                await guardar_mensaje(msg.telefono, "assistant", f"[INTERVENCION_HUMANA] {contenido_log}")
                logger.info(f"Intervención humana registrada para {msg.telefono}: {contenido_log}")
                continue

            if not msg.texto:
                continue

            texto = msg.texto.strip()

            # 2. Detectar "stop gian" (del cliente, caso raro pero por si acaso)
            if await es_comando_stop(texto):
                await pausar_contacto(msg.telefono)
                logger.info(f"Handoff activado para {msg.telefono} - Gian pausado")
                continue

            # 3. Detectar "start gian"
            if await es_comando_start(texto):
                await reanudar_contacto(msg.telefono)
                logger.info(f"Gian reanudado manualmente para {msg.telefono}")
                continue

            # 4. Si está pausado — no responder
            if await esta_pausado(msg.telefono):
                logger.info(f"Mensaje de {msg.telefono} ignorado - Gian pausado")
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
