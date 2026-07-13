# agent/crm.py
import os
import re
import httpx
import logging
from datetime import datetime

logger = logging.getLogger("agentkit")


def extraer_telefono(telefono: str) -> str:
    return telefono.replace("@s.whatsapp.net", "").replace("@c.us", "")


def limpiar_tags_cliente(texto: str) -> str:
    """Quita las etiquetas internas (LEAD, LEAD_MADERA, LLAMADA) antes de mostrarle el mensaje al cliente."""
    limpio = re.sub(r'\[LEAD:[^\]]+\]', '', texto)
    limpio = re.sub(r'\[LEAD_MADERA:[^\]]+\]', '', limpio)
    limpio = re.sub(r'\[LLAMADA:[^\]]+\]', '', limpio)
    return limpio.strip()


def extraer_datos_tag(historial: list) -> dict:
    for msg in reversed(historial):
        if msg["role"] == "assistant":
            match = re.search(r'\[LEAD:([^\]]+)\]', msg["content"])
            if match:
                datos = {}
                for par in match.group(1).split("|"):
                    if "=" in par:
                        clave, valor = par.split("=", 1)
                        datos[clave.strip()] = valor.strip()
                return datos
    return {}


def extraer_datos_tag_madera(historial: list) -> dict:
    for msg in reversed(historial):
        if msg["role"] == "assistant":
            match = re.search(r'\[LEAD_MADERA:([^\]]+)\]', msg["content"])
            if match:
                datos = {}
                for par in match.group(1).split("|"):
                    if "=" in par:
                        clave, valor = par.split("=", 1)
                        datos[clave.strip()] = valor.strip()
                return datos
    return {}


def formatear_historial(historial: list) -> str:
    """Convierte el historial de conversación en texto legible para el CRM."""
    lineas = ["=== HISTORIAL CONVERSACIÓN CON GIAN ===\n"]
    for msg in historial:
        rol = "Cliente" if msg["role"] == "user" else "Gian"
        # Limpiar tags internos del historial
        contenido = re.sub(r'\[LEAD:[^\]]+\]', '', msg["content"]).strip()
        contenido = re.sub(r'\[LEAD_MADERA:[^\]]+\]', '', contenido).strip()
        contenido = re.sub(r'\[LLAMADA:[^\]]+\]', '', contenido).strip()
        if contenido:
            lineas.append(f"{rol}: {contenido}")
    return "\n".join(lineas)


async def enviar_lead_crm(telefono: str, nombre: str, historial: list) -> bool:
    """Envía lead calificado al CRM con historial completo de conversación."""
    crm_url = os.getenv("CRM_WEBHOOK_URL")
    if not crm_url:
        logger.warning("CRM_WEBHOOK_URL no configurado")
        return False

    telefono_limpio = extraer_telefono(telefono)
    datos = extraer_datos_tag(historial)

    nombre_final = datos.get("nombre") or nombre or f"Lead {telefono_limpio}"
    comuna = datos.get("zona", "")
    medidas = datos.get("medidas", "")
    tipo = datos.get("tipo", "")
    email = datos.get("email", "")

    # Resumen de datos del proyecto
    resumen = " | ".join(filter(None, [
        f"Comuna: {comuna}" if comuna else "",
        f"Medidas: {medidas}" if medidas else "",
        f"Tipo: {tipo}" if tipo else "",
        f"WhatsApp: {telefono_limpio}"
    ]))

    # Historial completo de la conversación
    historial_texto = formatear_historial(historial)
    notas = f"{resumen}\n\n{historial_texto}"

    payload = {
        "name": nombre_final,
        "phone": telefono_limpio,
        "email": email or None,
        "source": "WhatsApp - Gian",
        "notes": notas,
        "comuna": comuna or None,
        "medidas": medidas or None,
    }

    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(crm_url, json=payload, timeout=10)
            if r.status_code in [200, 201]:
                logger.info(f"Lead enviado al CRM: {telefono_limpio} - {nombre_final}")
                return True
            else:
                logger.error(f"Error CRM: {r.status_code} - {r.text}")
                return False
    except Exception as e:
        logger.error(f"Error enviando al CRM: {e}")
        return False


async def enviar_contacto_incompleto_crm(telefono: str, nombre: str, historial: list) -> bool:
    """
    Guarda contactos que escribieron pero no completaron la calificación.
    Útil para campañas de remarketing posteriores.
    """
    crm_url = os.getenv("CRM_WEBHOOK_URL")
    if not crm_url:
        return False

    telefono_limpio = extraer_telefono(telefono)

    # Solo guardar si hay al menos 2 mensajes del cliente (conversación real)
    mensajes_cliente = [m for m in historial if m["role"] == "user"]
    if len(mensajes_cliente) < 2:
        return False

    nombre_final = nombre or f"Contacto {telefono_limpio}"
    historial_texto = formatear_historial(historial)
    notas = f"Contacto incompleto — no entregó todos los datos para cotizar.\n\n{historial_texto}"

    payload = {
        "name": nombre_final,
        "phone": telefono_limpio,
        "source": "WhatsApp - Remarketing",
        "notes": notas,
        "type": "incompleto"
    }

    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(crm_url, json=payload, timeout=10)
            if r.status_code in [200, 201]:
                logger.info(f"Contacto incompleto guardado en CRM: {telefono_limpio}")
                return True
            else:
                logger.error(f"Error CRM contacto incompleto: {r.status_code} - {r.text}")
                return False
    except Exception as e:
        logger.error(f"Error enviando contacto incompleto: {e}")
        return False


async def enviar_lead_distribuidor_crm(telefono: str, historial: list) -> bool:
    """Envía lead de madera al CRM para derivar a distribuidor."""
    crm_url = os.getenv("CRM_WEBHOOK_URL")
    if not crm_url:
        return False

    datos = extraer_datos_tag_madera(historial)
    if not datos:
        return False

    nombre = datos.get("nombre", "")
    apellido = datos.get("apellido", "")
    telefono_cliente = datos.get("telefono") or extraer_telefono(telefono)
    nombre_completo = f"{nombre} {apellido}".strip() or f"Lead Madera {telefono_cliente}"

    historial_texto = formatear_historial(historial)
    notas = f"Cliente interesado en pérgolas de madera — derivar a distribuidor autorizado.\n\n{historial_texto}"

    payload = {
        "name": nombre_completo,
        "phone": telefono_cliente,
        "source": "Distribuidor - Madera",
        "notes": notas,
        "type": "distribuidor",
    }

    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(crm_url, json=payload, timeout=10)
            if r.status_code in [200, 201]:
                logger.info(f"Lead distribuidor enviado al CRM: {telefono_cliente}")
                return True
            else:
                logger.error(f"Error CRM distribuidor: {r.status_code} - {r.text}")
                return False
    except Exception as e:
        logger.error(f"Error enviando lead distribuidor: {e}")
        return False
