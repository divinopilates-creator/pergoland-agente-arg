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

# Cache de mensajes procesados para evitar duplicados de Whapi
mensajes_procesados: set[str] = set()

# Mensajes automáticos de WhatsApp Business (Meta) que llegan como from_me=True
# pero NO fueron escritos por Gabriel manualmente — no deben pausar a Matías
MENSAJES_AUTOMATICOS_META = [
    "Gracias por contactarte con Pergoland Chile",
    "Gracias por contactarte con Pergoland Argentina",
]


def es_mensaje_automatico_meta(texto: str) -> bool:
    if not texto:
        return False
    return any(texto.strip().startswith(p) for p in MENSAJES_AUTOMATICOS_META)


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
    version="1.6.0",
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

            # 1. Mensajes propios (from_me=True) — pueden ser:
            #    a) Eco del propio mensaje que Matías acaba de enviar (Whapi lo
            #       reenvía como webhook). Hay que ignorarlo sin pausar nada.
            #    b) Mensaje automático de ausencia/saludo de WhatsApp Business
            #       (Meta) — tampoco fue escrito por Gabriel, no debe pausar.
            #    c) Gabriel Varela escribiendo manualmente o enviando un archivo.
            #       En ese caso se registra como intervención humana Y se pausa
            #       a Matías automáticamente (igual que "stop matias"). Matías
            #       solo vuelve a responder si alguien manda "start matias"
            #       explícitamente.
            if msg.es_propio:
                texto_humano = msg.texto.strip() if msg.texto else ""

                # "stop matias" / "start matias" siguen funcionando igual
                if texto_humano and await es_comando_stop(texto_humano):
                    await pausar_contacto(msg.telefono)
                    logger.info(f"Handoff activado para {msg.telefono} - Matias pausado (orden manual)")
                    continue
                if texto_humano and await es_comando_start(texto_humano):
                    await reanudar_contacto(msg.telefono)
                    logger.info(f"Matias reanudado para {msg.telefono} (orden manual)")
                    continue

                # Detectar si es el eco del propio bot: comparamos contra la última
                # respuesta que Matías generó y guardó para este contacto.
                historial_previo = await obtener_historial(msg.telefono, limite=5)
                ultima_respuesta_bot = ""
                for m in reversed(historial_previo):
                    if m["role"] == "assistant" and not m["content"].startswith("[INTERVENCION_HUMANA]"):
                        ultima_respuesta_bot = m["content"].strip()
                        break

                es_eco_del_bot = bool(texto_humano) and texto_humano == ultima_respuesta_bot
                es_auto_meta = es_mensaje_automatico_meta(texto_humano)

                if es_eco_del_bot or es_auto_meta:
                    motivo = "eco del bot" if es_eco_del_bot else "mensaje automático WhatsApp Business"
                    logger.info(f"Mensaje ignorado ({motivo}) para {msg.telefono}")
                    continue

                # Intervención humana real: registrar Y pausar automáticamente
                contenido_log = texto_humano if texto_humano else "Se envió un archivo"
                await guardar_mensaje(msg.telefono, "assistant", f"[INTERVENCION_HUMANA] {contenido_log}")
                await pausar_contacto(msg.telefono)
                logger.info(f"Intervención humana registrada y Matias pausado para {msg.telefono}: {contenido_log}")
                continue

            if not msg.texto:
                continue

            texto = msg.texto.strip()

            # 2. Detectar "stop matias" (del cliente, caso raro pero por si acaso)
            if await es_comando_stop(texto):
                await pausar_contacto(msg.telefono)
                logger.info(f"Handoff activado para {msg.telefono} - Matias pausado")
                continue

            # 3. Detectar "start matias"
            if await es_comando_start(texto):
                await reanudar_contacto(msg.telefono)
                logger.info(f"Matias reanudado manualmente para {msg.telefono}")
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
