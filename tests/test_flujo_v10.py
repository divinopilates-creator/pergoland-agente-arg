# tests/test_flujo_v10.py — Prueba del flujo v10 sin WhatsApp ni Claude reales
# Simula webhooks de Whapi, respuestas de Gian y el paso del tiempo para los seguimientos.
# Uso: python tests/test_flujo_v10.py
import os
import sys
import asyncio
import tempfile
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_db = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{_db}"
os.environ["WHATSAPP_PROVIDER"] = "whapi"
os.environ.setdefault("ANTHROPIC_API_KEY", "test")

from fastapi.testclient import TestClient
from sqlalchemy import update
import agent.main as main
import agent.flujo as flujo
import agent.crm as crm
import agent.brain as brain
import agent.handoff as handoff
from agent.memory import Mensaje, async_session, obtener_historial_completo

ENVIADOS, LEADS = [], []
RESPUESTAS = []  # cola de respuestas simuladas de Gian


class ProveedorFalso:
    async def parsear_webhook(self, request):
        return await main.ProveedorWhapiReal.parsear_webhook(self, request)

    async def validar_webhook(self, request):
        return None

    async def enviar_mensaje(self, telefono, mensaje):
        ENVIADOS.append((telefono, mensaje))
        return True


async def respuesta_falsa(mensaje, historial):
    return RESPUESTAS.pop(0)


async def lead_falso(telefono, nombre, historial):
    LEADS.append((telefono, nombre, crm.extraer_datos_tag(historial)))
    return True


async def nada(*a, **k):
    return True

from agent.providers.whapi import ProveedorWhapi
main.ProveedorWhapiReal = ProveedorWhapi
main.proveedor = ProveedorFalso()
main.generar_respuesta = respuesta_falsa
brain.generar_respuesta = respuesta_falsa
flujo.enviar_lead_crm = lead_falso
crm.enviar_lead_crm = lead_falso
flujo.enviar_contacto_incompleto_crm = nada
flujo.enviar_lead_distribuidor_crm = nada

_id = 0


def whapi(client, chat, texto, from_me=False, tipo="text"):
    global _id
    _id += 1
    msg = {"id": f"m{_id}", "type": tipo, "chat_id": chat, "from_me": from_me, "from_name": "Cliente WA"}
    if tipo == "text":
        msg["text"] = {"body": texto}
    r = client.post("/webhook", json={"messages": [msg]})
    assert r.status_code == 200, r.text


CLIENTE = None


def correr(coro):
    """Ejecuta la corrutina en el mismo event loop que el servidor de prueba."""
    async def _f():
        return await coro
    return CLIENTE.portal.call(_f)


async def adelantar(chat, horas):
    """Simula el paso del tiempo corriendo hacia atrás los timestamps del chat."""
    from sqlalchemy import select
    async with async_session() as s:
        filas = (await s.execute(select(Mensaje).where(Mensaje.telefono == chat))).scalars().all()
        for m in filas:
            m.timestamp = m.timestamp - timedelta(hours=horas)
        await s.commit()


ok = 0


def check(cond, desc):
    global ok
    assert cond, "FALLA: " + desc
    ok += 1
    print("  OK -", desc)


GHL = "Hola Laura, soy Gian de Pergoland. Te escribo por tu interés en un proyecto de pérgola o galería. ¿En qué barrio/ciudad sería la obra?"

with TestClient(main.app) as c:
    CLIENTE = c
    # ── A. Calificado completo ────────────────────────────────────────
    print("A. Flujo calificado desde GHL")
    A = "5491100000001@s.whatsapp.net"
    whapi(c, A, GHL, from_me=True)
    h = correr(obtener_historial_completo(A))
    check(len(h) == 1 and h[0]["role"] == "assistant" and h[0]["content"] == GHL, "mensaje GHL guardado como turno de Gian")
    check(not correr(handoff.esta_pausado(A)), "mensaje GHL no pausa a Gian")
    whapi(c, A, GHL, from_me=True)
    check(len(correr(obtener_historial_completo(A))) == 1, "eco del mensaje GHL no se duplica")

    RESPUESTAS.append("¿Qué medidas tiene el espacio para el proyecto? Con las aproximadas alcanza, por ejemplo 6x4.")
    whapi(c, A, "Nordelta")
    check(ENVIADOS[-1][1].startswith("¿Qué medidas"), "Gian responde al barrio")
    whapi(c, A, ENVIADOS[-1][1], from_me=True)
    check(not correr(handoff.esta_pausado(A)), "eco de respuesta normal ignorado")

    RESPUESTAS.append("Listo Laura, le paso tu disponibilidad a Martín y te confirma la reunión por acá. \U0001F64C\n[LEAD:nombre=Laura|telefono=5491100000001|email=|zona=Nordelta|medidas=6x5|tipo=galería]")
    whapi(c, A, "martes a la tarde")
    enviado = ENVIADOS[-1][1]
    check("[LEAD" not in enviado and "\U0001F64C" not in enviado, "cierre enviado sin tag ni emoji")
    check(enviado == "Listo Laura, le paso tu disponibilidad a Martín y te confirma la reunión por acá.", "texto de cierre exacto")
    check(correr(handoff.esta_pausado(A)), "Gian pausado después del cierre")
    check(len(LEADS) == 1 and LEADS[0][2]["zona"] == "Nordelta" and LEADS[0][2]["email"] == "", "lead enviado al CRM una vez, email vacío")
    whapi(c, A, enviado, from_me=True)
    check("[INTERVENCION_HUMANA]" not in str(correr(obtener_historial_completo(A))), "eco del cierre no se registra como intervención")
    n = len(ENVIADOS)
    whapi(c, A, "gracias! otra consulta")
    check(len(ENVIADOS) == n and len(LEADS) == 1, "después del cierre no responde ni reenvía lead")

    # ── B. Intervención humana real ───────────────────────────────────
    print("B. Intervención humana")
    B = "5491100000002@s.whatsapp.net"
    whapi(c, B, GHL.replace("Laura", "Pedro"), from_me=True)
    whapi(c, B, "Hola Pedro, te llamo en un rato. Martín", from_me=True)
    check(correr(handoff.esta_pausado(B)), "mensaje manual de Martín pausa a Gian")
    check("[INTERVENCION_HUMANA]" in correr(obtener_historial_completo(B))[-1]["content"], "queda registrado como intervención")
    whapi(c, B, "start gian", from_me=True)
    check(not correr(handoff.esta_pausado(B)), "start gian reanuda")

    # ── C. Llamada ────────────────────────────────────────────────────
    print("C. Llamada")
    C = "5491100000003@s.whatsapp.net"
    whapi(c, C, GHL.replace("Laura", "Ana"), from_me=True)
    RESPUESTAS.append("Para avanzar con tu proyecto, un asesor te va a llamar a este número a la brevedad.\n[LLAMADA:nombre=Ana|telefono=5491100000003]")
    whapi(c, C, "Villa Gesell")
    check("[LLAMADA" not in ENVIADOS[-1][1], "mensaje de llamada sin tag")
    check(correr(handoff.esta_pausado(C)), "LLAMADA pausa a Gian")
    check(LEADS[-1][1] == "Ana", "aviso al CRM con el nombre de la llamada")

    # ── D. Seguimientos 24/48/72 ──────────────────────────────────────
    print("D. Seguimientos")
    D = "5491100000004@s.whatsapp.net"
    whapi(c, D, GHL.replace("Laura", "Sofía"), from_me=True)
    RESPUESTAS.append("¿Qué medidas tiene el espacio para el proyecto? Con las aproximadas alcanza, por ejemplo 6x4.")
    whapi(c, D, "Pilar")
    n, nl = len(ENVIADOS), len(LEADS)

    correr(adelantar(D, 23))
    correr(handoff.procesar_seguimientos(main.proveedor))
    check(len(ENVIADOS) == n, "a las 23h no hay seguimiento")

    correr(adelantar(D, 2))
    RESPUESTAS.append("Hola Sofía, ¿pudiste ver lo que te consulté? ¿Qué medidas tiene el espacio?")
    correr(handoff.procesar_seguimientos(main.proveedor))
    check(len(ENVIADOS) == n + 1 and "pudiste ver" in ENVIADOS[-1][1], "a las 24h sale el seguimiento 1")
    correr(handoff.procesar_seguimientos(main.proveedor))
    check(len(ENVIADOS) == n + 1, "no repite el seguimiento 1")
    whapi(c, D, ENVIADOS[-1][1], from_me=True)
    check(not correr(handoff.esta_pausado(D)), "eco del seguimiento ignorado")

    correr(adelantar(D, 24))
    RESPUESTAS.append("Hola Sofía, te escribo por tu proyecto. Cuando quieras lo retomamos por acá.")
    correr(handoff.procesar_seguimientos(main.proveedor))
    check(len(ENVIADOS) == n + 2 and "retomamos" in ENVIADOS[-1][1], "a las 48h sale el seguimiento 2")

    correr(adelantar(D, 24))
    RESPUESTAS.append("[LEAD:nombre=Sofía|telefono=5491100000004|email=|zona=Pilar|medidas=s/d|tipo=]")
    correr(handoff.procesar_seguimientos(main.proveedor))
    check(len(ENVIADOS) == n + 2, "a las 72h no se envía nada al cliente")
    check(len(LEADS) == nl + 1 and LEADS[-1][2]["medidas"] == "s/d", "a las 72h el lead va al CRM")
    check(correr(handoff.esta_pausado(D)), "a las 72h Gian queda pausado")

    # ── E. Respuesta tras seguimiento reinicia el conteo ──────────────
    print("E. Cliente responde después del seguimiento 1")
    E = "5491100000005@s.whatsapp.net"
    whapi(c, E, GHL.replace("Laura", "Juan"), from_me=True)
    RESPUESTAS.append("¿Qué medidas tiene el espacio para el proyecto?")
    whapi(c, E, "Tigre")
    correr(adelantar(E, 25))
    RESPUESTAS.append("Hola Juan, ¿pudiste ver lo que te consulté? ¿Qué medidas tiene el espacio?")
    correr(handoff.procesar_seguimientos(main.proveedor))
    RESPUESTAS.append("Si podés mandarnos fotos o videos del lugar nos ayuda mucho.")
    whapi(c, E, "6x4")
    n = len(ENVIADOS)
    correr(adelantar(E, 25))
    RESPUESTAS.append("Hola Juan, ¿pudiste ver lo que te consulté? ¿Podés mandarnos fotos?")
    correr(handoff.procesar_seguimientos(main.proveedor))
    check(len(ENVIADOS) == n + 1 and "pudiste ver" in ENVIADOS[-1][1], "vuelve a empezar por el seguimiento 1")

    # ── F. Sin respuesta al mensaje de GHL: no hay seguimientos ───────
    print("F. Cliente que nunca respondió")
    F = "5491100000006@s.whatsapp.net"
    whapi(c, F, GHL.replace("Laura", "Eva"), from_me=True)
    correr(adelantar(F, 100))
    n = len(ENVIADOS)
    correr(handoff.procesar_seguimientos(main.proveedor))
    check(len(ENVIADOS) == n, "sin respuesta del cliente no hay seguimientos")

    # ── G. Archivo del cliente y archivo de Martín ────────────────────
    print("G. Archivos")
    G = "5491100000007@s.whatsapp.net"
    whapi(c, G, GHL.replace("Laura", "Leo"), from_me=True)
    RESPUESTAS.append("Recibimos el material. ¿En qué barrio sería la obra?")
    whapi(c, G, "", tipo="image")
    check(correr(obtener_historial_completo(G))[-2]["content"] == "[ARCHIVO_RECIBIDO]", "foto del cliente llega como [ARCHIVO_RECIBIDO]")
    whapi(c, G, "", from_me=True, tipo="document")
    check(correr(handoff.esta_pausado(G)), "archivo enviado por Martín pausa a Gian")

    # ── H. Sin emojis ─────────────────────────────────────────────────
    print("H. Emojis")
    check(all(not flujo._EMOJIS.search(m) for _, m in ENVIADOS), "ningún mensaje enviado tiene emojis")
    check(not flujo._EMOJIS.search(handoff.MSG_COTIZACION + handoff.MSG_VISITA), "recordatorios sin emojis")

print(f"\nTodas las pruebas pasaron ({ok} verificaciones).")
