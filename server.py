"""
GoContact MCP Server
====================

Servidor MCP (solo lectura) que conecta Claude con la API de GoContact
(Report Designer) para consultar leads, ventas y actividad del contact center
en lenguaje natural, sin esperar a informes manuales en Excel o Power BI.

Herramientas:
  datos_informe      -> Datos diarios compactos (leads, ventas, agentes) para un informe
  probar_conexion    -> Diagnóstico de token e informe de prueba
  listar_bases       -> Bases de datos de GoContact (número, nombre, activa, registros)
  leads_mes          -> Leads cargados en el mes, agregados por base / formulario / proveedor
  leads_detalle      -> Detalle por lead, cruzado con llamadas para marcar "venta segura"
  ventas_mes         -> Ventas registradas en el script de las llamadas (campañas y colas)
  resumen_agentes    -> Por agente: llamadas, contestadas, minutos y ventas
  llamadas_servicio  -> Llamadas de campañas y colas clasificadas como "servicio"
  columnas_informe   -> Cabeceras reales del CSV, para ajustar la configuración

Endpoints de GoContact usados:
  POST /auth/token                                (Basic auth -> token 24 h)
  POST /api/reportdesigner/generateReport         (genera el informe en diferido)
  GET  /api/reportdesigner/{reportJobId}/download (CSV; 202 = aún procesando)
  GET  /api/databases
  GET  /api/contacts/?ids=[...]
  GET  /api/owners/{ownerType}

Las reglas de negocio (qué es una venta, qué llamada está contestada, cómo se
asigna el proveedor, qué campañas son de servicio, nombres de columnas...)
viven en config/business_rules.json. Ver README.md y docs/reglas-de-negocio.md.

Requisitos:  pip install -r requirements.txt   (SDK de MCP 1.x)
"""

import calendar
import csv
import io
import json
import os
from pathlib import Path
import time
from collections import Counter, defaultdict

import httpx
try:
    from mcp.server.fastmcp import FastMCP
except ModuleNotFoundError as e:
    raise SystemExit('Este servidor necesita el SDK de MCP 1.x. Instálalo con: '
                     'python -m pip install "mcp[cli]<2"') from e

# --------------------------------------------------------------------------
# Configuración: credenciales y plantillas (entorno) + reglas de negocio (JSON)
# --------------------------------------------------------------------------
GOCONTACT_BASE = os.environ["GOCONTACT_BASE"]          # p. ej. https://TU-DOMINIO.gocontact.es/poll
GOCONTACT_USER = os.environ["GOCONTACT_USER"]
GOCONTACT_PASS = os.environ["GOCONTACT_PASS"]
TEMPLATE_LEADS = int(os.environ["GC_TEMPLATE_LEADS"])
TEMPLATE_VENTAS = int(os.environ["GC_TEMPLATE_VENTAS"])
TEMPLATE_LLAMADAS = int(os.environ["GC_TEMPLATE_LLAMADAS"])
# Si las plantillas de colas son distintas a las de campañas, se indican aparte.
TEMPLATE_COLAS = {
    TEMPLATE_VENTAS: int(os.environ.get("GC_TEMPLATE_VENTAS_COLAS") or TEMPLATE_VENTAS),
    TEMPLATE_LLAMADAS: int(os.environ.get("GC_TEMPLATE_LLAMADAS_COLAS") or TEMPLATE_LLAMADAS),
}


def _ids_env(nombre: str) -> list[int]:
    return [int(x) for x in os.environ.get(nombre, "").split(",") if x.strip()]


OWNER_IDS = {"campaign": _ids_env("GC_CAMPAIGN_IDS"), "queue": _ids_env("GC_QUEUE_IDS")}

DATATYPE_ALL_CALLS = 0
DATATYPE_CALLS_WITH_SCRIPT = 1
DATATYPE_CONTACT_STATE_BY_LOAD_DATE = 5

MAX_ESPERA_INFORME_S = 600  # 10 min como máximo esperando a que se genere un informe

CONFIG_PATH = Path(os.environ.get("GC_CONFIG",
                                  Path(__file__).parent / "config" / "business_rules.json"))
if not CONFIG_PATH.exists():
    raise SystemExit(f"No encuentro {CONFIG_PATH}. Copia config/business_rules.example.json "
                     "a config/business_rules.json y ajústalo a tu cuenta de GoContact.")
CFG = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))

COL = CFG["columnas"]                              # clave interna -> cabecera del CSV
CAMPOS_FECHA = set(CFG.get("campos_fecha", []))    # se normalizan a DD/MM/AAAA
VENTA_CAMPOS = CFG["venta_campos"]                 # campos devueltos por cada venta
VENTA_GRUPO = CFG["venta"]["grupo"]
VENTA_FINAL = CFG["venta"]["productos"]
VENTA_GESTION = CFG["venta"]["gestiones"]
VENTA_OUTCOME = CFG["venta"]["outcomes_venta"]
BASES_LEADS = CFG["bases_leads"]
OUTCOMES_NO_CONTESTADA = CFG["outcomes_no_contestada"]
SERVICIO_EMPIEZA_POR = CFG["servicio"]["empieza_por"]
SERVICIO_EXACTO = CFG["servicio"]["exacto"]
ORIGENES_NO_VALIDOS = {o.casefold() for o in CFG.get("origenes_no_validos", [])}
PROVEEDORES = [tuple(p) for p in CFG["proveedores"]]  # [[texto a buscar, nombre normalizado]]
PROVEEDOR_ALIAS = {k.casefold(): v for k, v in CFG.get("proveedor_alias", {}).items()}

COLS_SALIDA_VENTAS = ["fecha_llamada", "owner", "database", "database_nombre",
                      "id_formulario", "producto"] + VENTA_CAMPOS

# Por seguridad escucha solo en local salvo que se indique otra cosa (HOST=0.0.0.0 detrás de un proxy con OAuth)
mcp = FastMCP("gocontact-mcp", host=os.environ.get("HOST", "127.0.0.1"),
              port=int(os.environ.get("PORT", "8000")))
_http = httpx.Client(base_url=GOCONTACT_BASE, timeout=60)
_token = {"valor": None, "caduca": 0.0}


# --------------------------------------------------------------------------
# Acceso a GoContact
# --------------------------------------------------------------------------
def _cabeceras() -> dict:
    """Devuelve la cabecera Authorization, renovando el token si hace falta."""
    if not _token["valor"] or time.time() > _token["caduca"]:
        r = _http.post("/auth/token", auth=(GOCONTACT_USER, GOCONTACT_PASS))
        r.raise_for_status()
        _token["valor"] = _extraer_token(r)
        if not _token["valor"]:
            raise RuntimeError(f"/auth/token no devolvió un token reconocible: {r.text[:300]}")
        _token["caduca"] = time.time() + 23 * 3600  # caduca a las 24 h; renovamos antes
    return {"Authorization": f"Bearer {_token['valor']}"}


def _extraer_token(r) -> str:
    """El token puede llegar como texto plano, como cadena JSON o dentro de un objeto."""
    try:
        datos = r.json()
    except ValueError:
        return r.text.strip().strip('"')
    if isinstance(datos, str):
        return datos.strip()
    if isinstance(datos, dict):
        for clave in ("token", "access_token", "accessToken", "jwt", "data"):
            v = datos.get(clave)
            if isinstance(v, str) and v:
                return v
            if isinstance(v, dict):
                for c2 in ("token", "access_token", "accessToken"):
                    if isinstance(v.get(c2), str):
                        return v[c2]
    return ""


def _job_id(r) -> str:
    """reportJobId puede venir en un objeto o como texto."""
    try:
        datos = r.json()
    except ValueError:
        return r.text.strip().strip('"')
    if isinstance(datos, dict):
        for clave in ("reportJobId", "jobId", "id", "data"):
            v = datos.get(clave)
            if isinstance(v, (str, int)) and str(v):
                return str(v)
            if isinstance(v, dict) and v.get("reportJobId"):
                return str(v["reportJobId"])
    if isinstance(datos, (str, int)):
        return str(datos)
    raise RuntimeError(f"generateReport no devolvió reportJobId: {r.text[:300]}")


def _rango_mes(anio: int, mes: int, meses_extra: int = 0) -> tuple[str, str]:
    """Del día 1 del mes indicado al último día del mes + meses_extra (máx. 2: límite de 3 meses)."""
    meses_extra = max(0, min(meses_extra, 2))
    total = anio * 12 + (mes - 1) + meses_extra
    a_fin, m_fin = divmod(total, 12)
    m_fin += 1
    ultimo = calendar.monthrange(a_fin, m_fin)[1]
    return f"{anio}-{mes:02d}-01 00:00:00", f"{a_fin}-{m_fin:02d}-{ultimo:02d} 24:00:00"


_cache_informes: dict[tuple, tuple[float, list[dict]]] = {}
CACHE_SEGUNDOS = 30 * 60  # un informe ya descargado se reutiliza durante 30 minutos


def _clave(owner_type, data_type, template_id, anio, mes, meses_extra=0):
    return (owner_type, data_type, template_id, anio, mes, meses_extra)


def _lanzar(owner_type, data_type, template_id, anio, mes, meses_extra=0) -> str:
    """Pide a GoContact que genere un informe y devuelve su reportJobId (no espera)."""
    r = _http.post("/api/reportdesigner/generateReport",
                   json=_cuerpo_informe(owner_type, data_type, template_id, anio, mes, meses_extra),
                   headers=_cabeceras())
    if r.status_code not in (200, 202):
        raise RuntimeError(f"GoContact generateReport {r.status_code}: {r.text}")
    return _job_id(r)


def _descargar_varios(trabajos: dict) -> None:
    """trabajos: {clave: reportJobId}. Espera a todos a la vez y los guarda en la caché."""
    pendientes, limite = dict(trabajos), time.time() + MAX_ESPERA_INFORME_S
    while pendientes and time.time() < limite:
        for clave, job in list(pendientes.items()):
            d = _http.get(f"/api/reportdesigner/{job}/download", headers=_cabeceras())
            if d.status_code == 200:
                _cache_informes[clave] = (time.time(), _parsear_csv(d.content))
                del pendientes[clave]
            elif d.status_code != 202:
                raise RuntimeError(f"GoContact download {d.status_code}: {d.text}")
        if pendientes:
            time.sleep(10)
    if pendientes:
        raise TimeoutError("Algún informe de GoContact no estuvo listo a tiempo.")


def _precargar(especificaciones: list[tuple]) -> None:
    """Lanza en paralelo los informes que no estén en caché y espera a todos."""
    trabajos = {}
    for esp in especificaciones:
        clave = _clave(*esp)
        if clave in _cache_informes and time.time() - _cache_informes[clave][0] < CACHE_SEGUNDOS:
            continue
        trabajos[clave] = _lanzar(*esp)
    if trabajos:
        _descargar_varios(trabajos)


def _generar_y_descargar(owner_type: str, data_type: int, template_id: int,
                         anio: int, mes: int, meses_extra: int = 0) -> list[dict]:
    """Devuelve las filas de un informe del Report Designer (de la caché o generándolo)."""
    _precargar([(owner_type, data_type, template_id, anio, mes, meses_extra)])
    return [dict(f) for f in _cache_informes[_clave(owner_type, data_type, template_id, anio, mes, meses_extra)][1]]


def _cuerpo_informe(owner_type, data_type, template_id, anio, mes, meses_extra=0) -> dict:
    inicio, fin = _rango_mes(anio, mes, meses_extra)
    if owner_type == "queue":
        template_id = TEMPLATE_COLAS.get(template_id, template_id)
    cuerpo = {
        "ownerType": owner_type,
        "startDate": inicio,
        "endDate": fin,
        "dataType": data_type,
        "templateId": template_id,
        "language": "es",
    }
    if OWNER_IDS[owner_type]:
        cuerpo["ownerId"] = OWNER_IDS[owner_type]
    else:
        cuerpo["ownerId"] = ["allOwners"]
        cuerpo["includeALLOwners"] = True
    return cuerpo


def _parsear_csv(contenido: bytes) -> list[dict]:
    texto = contenido.decode("utf-8-sig")
    try:
        dialecto = csv.Sniffer().sniff(texto[:5000], delimiters=";,")
    except csv.Error:
        dialecto = csv.excel
    lector = csv.DictReader(io.StringIO(texto), dialect=dialecto)
    lector.fieldnames = [h.strip() for h in (lector.fieldnames or [])]  # p. ej. "232-81 Asegurados 1 "
    return list(lector)


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------
def _v(fila: dict, clave: str) -> str:
    valor = (fila.get(COL[clave]) or "").strip()
    if not valor and clave == "call_uuid":
        valor = (fila.get(COL["call_uuid_script"]) or "").strip()
    if clave in CAMPOS_FECHA:
        return _fecha(valor)
    return valor


def _fecha(valor: str) -> str:
    """Normaliza fechas a DD/MM/AAAA (acepta 09/07/2026, 9-7-2026 o 11072026)."""
    digitos = "".join(c for c in valor if c.isdigit())
    if "/" not in valor and "-" not in valor and len(digitos) == 8:
        return f"{digitos[:2]}/{digitos[2:4]}/{digitos[4:]}"
    partes = valor.replace("-", "/").split("/")
    if len(partes) == 3 and all(x.isdigit() for x in partes) and len(partes[2]) == 4:
        return f"{int(partes[0]):02d}/{int(partes[1]):02d}/{partes[2]}"
    return valor


def _owner(fila: dict) -> str:
    """Nombre de la campaña o cola de la fila."""
    return _v(fila, "owner") or _v(fila, "owner_cola") or fila.get("_owner_nombre", "")


def _norm(texto: str) -> str:
    return " ".join(texto.casefold().split())


def _en(valor: str, permitidos: list[str]) -> bool:
    return _norm(valor) in {_norm(p) for p in permitidos}


def _es_servicio(nombre: str) -> bool:
    n = _norm(nombre)
    return (any(n.startswith(_norm(p)) for p in SERVICIO_EMPIEZA_POR)
            or n in {_norm(e) for e in SERVICIO_EXACTO})


def _filtrar_owner(filas: list[dict], nombres: list[str] | None) -> list[dict]:
    """Filtra por nombre de campaña/cola (coincidencia parcial, sin mayúsculas)."""
    if not nombres:
        return filas
    buscados = [_norm(n) for n in nombres]
    return [f for f in filas if any(b in _norm(_owner(f)) for b in buscados)]


def _filtrar_bases(filas: list[dict], database_ids: list[int] | None) -> list[dict]:
    """Sin database_ids devuelve todas las bases; con ellos, solo esas."""
    if not database_ids:
        return filas
    ids = {str(i) for i in database_ids}
    return [f for f in filas if _v(f, "database") in ids]


def _es_venta(f: dict, grupo=None, productos=None, gestiones=None) -> bool:
    return (_en(_v(f, "grupo"), grupo or VENTA_GRUPO)
            and _en(_v(f, "final"), productos or VENTA_FINAL)
            and _en(_v(f, "gestion"), gestiones or VENTA_GESTION))


_cache_owners: dict[str, dict[str, str]] = {}


def _nombres_owners(owner_type: str) -> dict[str, str]:
    """Mapa Owner ID -> nombre (GET /api/owners/{ownerType}). Se cachea en memoria."""
    if owner_type not in _cache_owners:
        r = _http.get(f"/api/owners/{owner_type}", headers=_cabeceras())
        r.raise_for_status()
        datos = r.json()
        # La forma exacta de la respuesta no está documentada: se aceptan varias
        if isinstance(datos, dict):
            lista = datos.get("data") or datos.get("owners") or datos.get(owner_type) or datos
        else:
            lista = datos
        mapa = {}
        if isinstance(lista, dict):  # {"31": "Nombre", ...} o {"31": {...}}
            for k, v in lista.items():
                mapa[str(k)] = v if isinstance(v, str) else (v.get("name", "") if isinstance(v, dict) else "")
        elif isinstance(lista, list):
            for o in lista:
                if isinstance(o, dict):
                    mapa[str(o.get("id", o.get("owner_id", "")))] = o.get("name", o.get("owner_name", ""))
        _cache_owners[owner_type] = mapa
    return _cache_owners[owner_type]


def _llamadas_campanas_y_colas(data_type: int, template_id: int,
                               anio: int, mes: int, meses_extra: int = 0) -> list[dict]:
    """Llamadas de campañas (salientes) y colas (entrantes) del periodo, juntas.

    Cada fila es un tramo de llamada atendido por un agente. En colas, una misma
    llamada (UUID) puede tener varios tramos por transferencias entre agentes/colas:
    se conservan todos, porque cada uno es trabajo de un agente. Solo se quitan las
    filas repetidas (mismo UUID, agente, owner y hora de inicio)."""
    filas, vistos = [], set()
    for owner_type in ("campaign", "queue"):
        nombres = None
        for f in _generar_y_descargar(owner_type, data_type, template_id, anio, mes, meses_extra):
            clave = (_v(f, "call_uuid"), _v(f, "agente"), _v(f, "owner_id"), _v(f, "fecha_llamada"))
            if clave in vistos:
                continue
            vistos.add(clave)
            f["_tipo"] = "campaña" if owner_type == "campaign" else "cola"
            if not _owner(f):  # solo si el informe no trae el nombre de la cola
                if nombres is None:
                    try:
                        nombres = _nombres_owners(owner_type)
                    except Exception:
                        nombres = {}
                f["_owner_nombre"] = nombres.get(_v(f, "owner_id"), f"Cola {_v(f, 'owner_id')}")
            filas.append(f)
    return filas


# Campos extra del contacto: si el Report Designer no los trae, se piden a /api/contacts/
CAMPOS_EXTRA = ["unique_id", "id_formulario", "proveedor", "producto", "medio",
                "servicio", "venta_vd"]


def _sin_duplicados(filas: list[dict]) -> list[dict]:
    """El informe puede repetir un mismo Contact ID; nos quedamos con una fila por contacto."""
    vistos, salida = set(), []
    for f in filas:
        cid = _v(f, "contact_id")
        if cid and cid in vistos:
            continue
        vistos.add(cid)
        salida.append(f)
    return salida


def _enriquecer_campos_extra(filas: list[dict], lote: int = 200) -> list[dict]:
    """Añade a cada fila los campos extra del contacto (IdFormulario, Proveedor, Producto...)
    consultando GET /api/contacts/?ids=[...] en lotes."""
    # Si el Report Designer ya trae los campos extra (columnas "[fieldN]"), no hace falta la API
    if filas and COL["id_formulario"] in filas[0]:
        return filas
    ids = [_v(f, "contact_id") for f in filas if _v(f, "contact_id").isdigit()]
    extra_por_id = {}
    for i in range(0, len(ids), lote):
        trozo = [int(x) for x in ids[i:i + lote]]
        r = _http.get("/api/contacts/", params={"ids": json.dumps(trozo)}, headers=_cabeceras())
        r.raise_for_status()
        for c in r.json():
            extra_por_id[str(c.get("id"))] = {**(c.get("fields") or {}),
                                              "_load_date": c.get("load_date") or ""}
    for f in filas:
        extra = extra_por_id.get(_v(f, "contact_id"), {})
        for clave in CAMPOS_EXTRA:
            if not f.get(COL[clave]):
                f[COL[clave]] = str(extra.get(COL[clave], "") or "")
        if not f.get(COL["load_date"]):
            f[COL["load_date"]] = extra.get("_load_date", "")
    return filas


def _leads_del_mes(anio: int, mes: int, database_ids: list[int] | None) -> list[dict]:
    """Leads cargados en el mes: informe de Report Designer + filtro de bases +
    quitar duplicados + campos extra desde la API de contactos."""
    filas = _generar_y_descargar("campaign", DATATYPE_CONTACT_STATE_BY_LOAD_DATE,
                                 TEMPLATE_LEADS, anio, mes)
    filas = _enriquecer_campos_extra(_sin_duplicados(_filtrar_bases(filas, database_ids)))
    for f in filas:  # proveedor normalizado a partir del id formulario
        f[COL["proveedor"]] = _proveedor(_v(f, "id_formulario"), _v(f, "proveedor"))
    return filas


def _contestada(f: dict) -> bool:
    return (not _en(_v(f, "outcome_llamada"), OUTCOMES_NO_CONTESTADA)
            and _a_int(_v(f, "talk_time")) > 0)


def _proveedor(idf: str, campo_proveedor: str = "") -> str:
    """Manda el id formulario. El campo proveedor solo se usa si no hay id formulario,
    porque viene sin normalizar (validado contra el cierre mensual manual)."""
    if idf:
        t = _norm(idf)
        for clave, nombre in PROVEEDORES:
            if clave in t:
                return nombre
        return "Otros / propios"
    c = _norm(campo_proveedor)
    if c in PROVEEDOR_ALIAS:
        return PROVEEDOR_ALIAS[c]
    for clave, nombre in PROVEEDORES:
        if clave in c:
            return nombre
    return "(sin id formulario)"


def _idf_venta(f: dict, idf_por_contacto: dict | None = None) -> str:
    """id formulario de una llamada con venta: campo del contacto, si no el del lead
    (por Contact ID) y, si no, el origen de la llamada (3-81 saliente / 231-81 entrante).
    Con esta cascada, en la validación todas las ventas quedaron con id formulario."""
    idf = _v(f, "id_formulario")
    if not idf and idf_por_contacto:
        idf = idf_por_contacto.get(_v(f, "contact_id"), "")
    for origen in ("origen_saliente", "origen_entrante"):
        if not idf and _norm(_v(f, origen)) not in ORIGENES_NO_VALIDOS:
            idf = _v(f, origen)
    return idf


def _a_iso(valor: str) -> str:
    """Fecha a AAAA-MM-DD (acepta 2026-09-03 10:00, 03/09/2026 10:00 o 03092026)."""
    v = (valor or "").strip()
    if len(v) >= 10 and v[4] == "-" and v[7] == "-":
        return v[:10]
    f = _fecha(v.split(" ")[0])
    return f"{f[6:10]}-{f[3:5]}-{f[0:2]}" if len(f) == 10 and f[2] == "/" else ""


def _dia_carga(f: dict) -> str:
    # Ojo: "DB Created Date" es la fecha de creación de la BASE, no del lead: no sirve.
    return _a_iso(_v(f, "load_date"))


def _fechas_carga_api(ids: list[str], lote: int = 200) -> dict[str, str]:
    """Fecha de carga real (load_date) de cada contacto, desde GET /api/contacts/?ids=[...]."""
    fechas = {}
    validos = [int(i) for i in ids if str(i).isdigit()]
    for i in range(0, len(validos), lote):
        r = _http.get("/api/contacts/", params={"ids": json.dumps(validos[i:i + lote])},
                      headers=_cabeceras())
        r.raise_for_status()
        datos = r.json()
        for c in (datos if isinstance(datos, list) else datos.get("data", [])):
            if isinstance(c, dict) and c.get("load_date"):
                fechas[str(c.get("id"))] = _a_iso(str(c["load_date"]))
    return fechas


def _a_int(texto: str) -> int:
    try:
        return int(float(texto.replace(",", ".")))
    except ValueError:
        return 0


# --------------------------------------------------------------------------
# Herramientas MCP (solo lectura)
# --------------------------------------------------------------------------
@mcp.tool()
def datos_informe(anio: int, mes: int, database_ids: list[int] | None = None,
                  parte: str = "todo") -> dict:
    """Datos diarios y compactos para el informe HTML de seguimiento (leads, ventas,
    agentes y producto vendido) de un mes. Una sola llamada.

    database_ids: bases de leads (por defecto las de la validación: BASES_LEADS).
    Devuelve listas de filas:
      leads:   [día de carga, proveedor, id formulario, nº leads]
      ventas:  [día de la venta, proveedor, id formulario, agente, ramo (152-81), productos "a|b"]
      agentes: [día, agente, gestionadas, contestadas, ventas por outcome]
    Las ventas son ventas seguras (151/152/153-81) de campañas y colas.
    parte: "todo" (por defecto), o solo "leads", "ventas" o "agentes" si se quiere
    repartir la carga en varias llamadas. Los informes se generan en paralelo y se
    guardan 30 minutos, así que repetir la consulta es rápido.
    """
    esp = []
    if parte in ("todo", "leads", "ventas"):
        esp.append(("campaign", DATATYPE_CONTACT_STATE_BY_LOAD_DATE, TEMPLATE_LEADS, anio, mes))
    if parte in ("todo", "ventas"):
        esp += [(t, DATATYPE_CALLS_WITH_SCRIPT, TEMPLATE_VENTAS, anio, mes) for t in ("campaign", "queue")]
    if parte in ("todo", "agentes"):
        esp += [(t, DATATYPE_ALL_CALLS, TEMPLATE_LLAMADAS, anio, mes) for t in ("campaign", "queue")]
    _precargar(esp)
    resultado = {"periodo": f"{anio}-{mes:02d}", "bases": database_ids or BASES_LEADS}
    leads = _leads_del_mes(anio, mes, database_ids or BASES_LEADS) if parte != "agentes" else []
    # Día de carga real de cada lead (el informe no lo trae): se pide a la API de contactos
    mes_txt = f"{anio}-{mes:02d}"
    sin_dia = [_v(f, "contact_id") for f in leads if not _dia_carga(f).startswith(mes_txt)]
    fechas = _fechas_carga_api(sin_dia) if sin_dia else {}
    def _dia(f):
        d = _dia_carga(f)
        return d if d.startswith(mes_txt) else fechas.get(_v(f, "contact_id"), mes_txt)
    cl = Counter((_dia(f), _v(f, "proveedor"), _v(f, "id_formulario") or "(vacío)") for f in leads)
    idf_por_contacto = {_v(f, "contact_id"): _v(f, "id_formulario") for f in leads}

    ventas = []
    filas_script = _llamadas_campanas_y_colas(DATATYPE_CALLS_WITH_SCRIPT, TEMPLATE_VENTAS, anio, mes) \
        if parte in ("todo", "ventas") else []
    for f in filas_script:
        if not _es_venta(f):
            continue
        idf = _idf_venta(f, idf_por_contacto)
        prods = "|".join(x for x in (_v(f, "prod_1"), _v(f, "prod_2"), _v(f, "prod_3")) if x)
        ventas.append([_a_iso(_v(f, "fecha_llamada")), _proveedor(idf, _v(f, "proveedor")),
                       idf or "(sin id formulario)", _v(f, "agente") or "(sin agente)",
                       _v(f, "final"), prods])

    ag = defaultdict(lambda: [0, 0, 0])
    filas_llamadas = _llamadas_campanas_y_colas(DATATYPE_ALL_CALLS, TEMPLATE_LLAMADAS, anio, mes) \
        if parte in ("todo", "agentes") else []
    for f in filas_llamadas:
        a = ag[(_a_iso(_v(f, "fecha_llamada")), _v(f, "agente") or "(sin agente)")]
        a[0] += 1
        a[1] += 1 if _contestada(f) else 0
        a[2] += 1 if _en(_v(f, "outcome_llamada"), VENTA_OUTCOME) else 0

    return {
        **resultado,
        "leads_total": len(leads),
        "leads_sin_fecha": sum(1 for f in leads if len(_dia(f)) != 10),
        "leads": [[d, p, i, n] for (d, p, i), n in sorted(cl.items())],
        "ventas": ventas,
        "agentes": [[d, a, *v] for (d, a), v in sorted(ag.items())],
    }


@mcp.tool()
def probar_conexion() -> dict:
    """Diagnóstico de la conexión con GoContact: comprueba el inicio de sesión (token)
    y lanza un informe pequeño de prueba. Devuelve en qué paso falla, si falla, y la
    respuesta de GoContact en ese paso (sin datos de contactos)."""
    import traceback
    pasos = {}
    try:
        r = _http.post("/auth/token", auth=(GOCONTACT_USER, GOCONTACT_PASS))
        pasos["token_http"] = r.status_code
        pasos["token_formato"] = type(r.json()).__name__ if r.headers.get("content-type", "").startswith("application/json") else "texto"
        pasos["token_ok"] = bool(_extraer_token(r))
        _token["valor"] = _extraer_token(r); _token["caduca"] = time.time() + 23 * 3600
    except Exception as e:
        pasos["token_error"] = f"{type(e).__name__}: {e}"
        return pasos
    try:
        dia = time.strftime("%Y-%m-01")  # primer día del mes en curso
        cuerpo = {"ownerType": "campaign", "ownerId": ["allOwners"], "includeALLOwners": True,
                  "startDate": f"{dia} 00:00:00", "endDate": f"{dia} 24:00:00",
                  "dataType": DATATYPE_ALL_CALLS, "templateId": TEMPLATE_LLAMADAS, "language": "es"}
        r = _http.post("/api/reportdesigner/generateReport", json=cuerpo, headers=_cabeceras())
        pasos["informe_http"] = r.status_code
        pasos["informe_respuesta"] = r.text[:300]
    except Exception as e:
        pasos["informe_error"] = f"{type(e).__name__}: {e}"
        pasos["donde"] = traceback.format_exc().strip().splitlines()[-3:]
    return pasos


@mcp.tool()
def listar_bases(solo_activas: bool = True, buscar: str | None = None) -> dict:
    """Lista las bases de datos de GoContact: número, nombre, campaña asociada,
    si está activa y número de registros. Sirve para saber qué bases existen
    antes de pedir leads de unas concretas.

    solo_activas: True (por defecto) para ver solo las activas.
    buscar: opcional, texto que debe contener el nombre de la base.
    """
    bases, siguiente = [], 1
    while True:
        params = {"database_id": siguiente, "limit": 500, "database_type": "user"}
        if solo_activas:
            params["active"] = "true"
        r = _http.get("/api/databases", params=params, headers=_cabeceras())
        r.raise_for_status()
        pagina = r.json()
        if not pagina:
            break
        bases.extend(pagina)
        if len(pagina) < 500:
            break
        siguiente = max(b["id"] for b in pagina) + 1

    if buscar:
        bases = [b for b in bases if _norm(buscar) in _norm(b.get("name", ""))]
    return {
        "total": len(bases),
        "bases": [
            {"database": b.get("id"), "nombre": b.get("name"),
             "owner_type": b.get("owner_type"), "owner_id": b.get("owner_id"),
             "activa": b.get("active"), "registros": b.get("counted_size")}
            for b in sorted(bases, key=lambda x: x.get("id", 0))
        ],
    }


@mcp.tool()
def leads_mes(anio: int, mes: int, database_ids: list[int] | None = None) -> dict:
    """Leads cargados en GoContact en el mes indicado (filtro por fecha de carga).

    database_ids: opcional. Si se indica, solo esas bases; si no, todas las bases
    de las campañas a las que tenga acceso el usuario.

    Devuelve conteos agregados por Database (número y nombre), id formulario,
    Producto, Outcome (en bruto, sin limpiar) y valor de Venta VD, más un
    resumen de leads por base. La limpieza de Outcomes, la regla de Venta VD y
    la asignación de proveedor/producto las aplica Claude según las
    instrucciones del proyecto (Producto se usa como respaldo cuando el
    id formulario no trae el producto).
    """
    filas = _leads_del_mes(anio, mes, database_ids)

    conteo = Counter(
        (_v(f, "database"), _v(f, "database_nombre"), _v(f, "id_formulario"),
         _v(f, "proveedor"), _v(f, "producto"), _v(f, "outcome"), _v(f, "venta_vd"))
        for f in filas
    )
    por_base = Counter((_v(f, "database"), _v(f, "database_nombre")) for f in filas)
    return {
        "periodo": f"{anio}-{mes:02d}",
        "bases_pedidas": database_ids or "todas",
        "total_leads": len(filas),
        "leads_por_base": [
            {"database": db, "nombre": nom, "leads": n}
            for (db, nom), n in sorted(por_base.items())
        ],
        "filas": [
            {"database": db, "database_nombre": nom, "id_formulario": idf, "proveedor": prov,
             "producto": prod, "outcome": out, "venta_vd": vvd, "leads": n}
            for (db, nom, idf, prov, prod, out, vvd), n in sorted(conteo.items())
        ],
    }


@mcp.tool()
def leads_detalle(
    anio: int,
    mes: int,
    database_ids: list[int] | None = None,
    proveedores: list[str] | None = None,
    cruzar_llamadas: bool = True,
    meses_ventana: int = 1,
    pagina: int = 1,
    tamano_pagina: int = 2000,
) -> dict:
    """Detalle fila a fila de los leads cargados en el mes (filtro por fecha de carga),
    cruzado por Contact ID con todas las llamadas realizadas a esos contactos.
    Pensado para generar los CSV de validación por proveedor.

    database_ids:   opcional, solo esas bases (si no, todas).
    proveedores:    opcional, proveedores a incluir (campo Proveedor o, si no, texto
                    del id formulario), p. ej. ["Afiliado A", "Comparador B"]; sin distinguir mayúsculas.
    cruzar_llamadas: True (por defecto) para cruzar con las llamadas y marcar venta segura.
    meses_ventana:  meses posteriores a la carga en los que se buscan llamadas
                    (0, 1 o 2; por defecto 1, es decir, mes de carga + el siguiente).
    pagina / tamano_pagina: paginación (tamano_pagina máximo 5000).

    Cada lead: contact_id, unique_id, campaña, database, database_nombre, load_date,
    id_formulario, proveedor, producto, medio, outcome (en bruto), venta_vd y, si se cruza:
    llamadas (nº total a ese contacto, campañas y colas), venta_segura (Sí/No)
    y ventas_seguras (nº de llamadas con venta).
    En la página 1 se añade "ventas_seguras": una fila por llamada con venta de
    los leads filtrados, con todos los campos de VENTA_CAMPOS.
    """
    tamano_pagina = max(1, min(tamano_pagina, 5000))
    leads = _leads_del_mes(anio, mes, database_ids)
    if proveedores:
        buscados = [_norm(p) for p in proveedores]
        leads = [f for f in leads
                 if any(b in _norm(_v(f, "proveedor")) or b in _norm(_v(f, "id_formulario"))
                        for b in buscados)]
    ids_leads = {_v(f, "contact_id") for f in leads}

    n_llamadas, ventas_por_contacto = Counter(), defaultdict(list)
    if cruzar_llamadas and ids_leads:
        for f in _llamadas_campanas_y_colas(DATATYPE_ALL_CALLS, TEMPLATE_LLAMADAS,
                                            anio, mes, meses_ventana):
            cid = _v(f, "contact_id")
            if cid in ids_leads:
                n_llamadas[cid] += 1
        for f in _llamadas_campanas_y_colas(DATATYPE_CALLS_WITH_SCRIPT, TEMPLATE_VENTAS,
                                            anio, mes, meses_ventana):
            cid = _v(f, "contact_id")
            if cid in ids_leads and _es_venta(f):
                ventas_por_contacto[cid].append(f)

    campos = ["contact_id", "unique_id", "owner", "database", "database_nombre",
              "load_date", "id_formulario", "proveedor", "producto", "medio",
              "outcome", "lead_status", "total_calls", "venta_vd"]
    inicio = (pagina - 1) * tamano_pagina
    salida = []
    for f in leads[inicio:inicio + tamano_pagina]:
        fila = {("campana" if c == "owner" else c): (_owner(f) if c == "owner" else _v(f, c))
                for c in campos}
        if cruzar_llamadas:
            cid = fila["contact_id"]
            fila["llamadas"] = n_llamadas.get(cid, 0)
            fila["ventas_seguras"] = len(ventas_por_contacto.get(cid, []))
            fila["venta_segura"] = "Sí" if fila["ventas_seguras"] else "No"
        salida.append(fila)

    resultado = {
        "periodo_carga": f"{anio}-{mes:02d}",
        "periodo_llamadas": " a ".join(_rango_mes(anio, mes, meses_ventana)) if cruzar_llamadas else None,
        "total_leads": len(leads),
        "leads_con_venta_segura": len(ventas_por_contacto),
        "pagina": pagina,
        "paginas_totales": max(1, -(-len(leads) // tamano_pagina)),
        "leads": salida,
    }
    if cruzar_llamadas and pagina == 1:
        resultado["ventas_seguras"] = [
            {**{c: _v(v, c) for c in VENTA_CAMPOS},
             "fecha_llamada": _v(v, "fecha_llamada"),
             "campana_o_cola": _owner(v), "tipo": v.get("_tipo", "")}
            for cid in sorted(ventas_por_contacto) for v in ventas_por_contacto[cid]
        ]
    return resultado


@mcp.tool()
def ventas_mes(
    anio: int,
    mes: int,
    campanas: list[str] | None = None,
    database_ids: list[int] | None = None,
    grupo: list[str] | None = None,
    productos: list[str] | None = None,
    gestiones: list[str] | None = None,
    incluir_detalle: bool = False,
) -> dict:
    """Ventas del mes registradas en llamadas de campañas y colas, según los campos de script
    151-81 Grupo, 152-81 Final (producto) y 153-81 Gestión realizada.

    Filtros opcionales:
      campanas      nombres (o parte del nombre) de campañas a incluir
      database_ids  bases de datos a incluir
    Criterios opcionales (si no se indican, se usan los valores por defecto):
      grupo         valores válidos de 151-81 (por defecto: Venta)
      productos     valores válidos de 152-81 (por defecto: los de la configuración)
      gestiones     valores válidos de 153-81 (por defecto: las de la configuración)
    Devuelve el resumen por producto y por campaña, y el detalle sin datos personales.
    """
    filas = _llamadas_campanas_y_colas(DATATYPE_CALLS_WITH_SCRIPT, TEMPLATE_VENTAS, anio, mes)
    filas = _filtrar_owner(filas, campanas)
    filas = _filtrar_bases(filas, database_ids)

    g, p, ge = grupo or VENTA_GRUPO, productos or VENTA_FINAL, gestiones or VENTA_GESTION
    ventas = [f for f in filas if _es_venta(f, g, p, ge)]
    for f in ventas:  # id formulario y proveedor normalizados en cada venta
        f[COL["id_formulario"]] = _idf_venta(f)
        f[COL["proveedor"]] = _proveedor(f[COL["id_formulario"]], _v(f, "proveedor"))
    return {
        "periodo": f"{anio}-{mes:02d}",
        "criterios": {"grupo": g, "productos": p, "gestiones": ge},
        "llamadas_analizadas": len(filas),
        "total_ventas": len(ventas),
        "ventas_por_producto": dict(Counter(_v(f, "final") for f in ventas).most_common()),
        "ventas_por_campana": dict(Counter(_owner(f) for f in ventas).most_common()),
        "ventas_por_agente": dict(Counter(_v(f, "agente") or "(sin agente)" for f in ventas).most_common()),
        "ventas_por_proveedor": dict(Counter(_v(f, "proveedor") or "(sin proveedor)" for f in ventas).most_common()),
        "ventas_por_base": dict(Counter(
            f"{_v(f, 'database')} {_v(f, 'database_nombre')}".strip() for f in ventas
        ).most_common()),
        "detalle": [{**{c: (_owner(f) if c == "owner" else _v(f, c)) for c in COLS_SALIDA_VENTAS},
                     "tipo": f.get("_tipo", "")}
                    for f in ventas] if incluir_detalle else "pide incluir_detalle=True para ver cada venta",
    }


@mcp.tool()
def resumen_agentes(
    anio: int,
    mes: int,
    agentes: list[str] | None = None,
    nombres: list[str] | None = None,
    solo_servicio: bool = False,
) -> dict:
    """Resumen por agente del mes: llamadas gestionadas, contestadas y ventas.

    agentes:       opcional, usuarios de agente a incluir (p. ej. ["agente01"]).
    nombres:       opcional, limita a campañas/colas cuyo nombre contenga estos textos.
    solo_servicio: True para contar solo campañas/colas clasificadas como servicio.

    Por agente devuelve: llamadas (total), en campañas y en colas, contestadas
    (outcome distinto de OUTCOMES_NO_CONTESTADA y con conversación), minutos de conversación, ventas (llamadas que
    cumplen los criterios de venta del script), % ventas sobre contestadas,
    ventas por producto y desglose de outcomes (en bruto, para que Claude aplique
    la limpieza si se quiere otra definición de "contestada").
    """
    def _incluir(f):
        if agentes and _norm(_v(f, "agente")) not in {_norm(a) for a in agentes}:
            return False
        if solo_servicio and not _es_servicio(_owner(f)):
            return False
        return True

    llamadas = [f for f in _filtrar_owner(
        _llamadas_campanas_y_colas(DATATYPE_ALL_CALLS, TEMPLATE_LLAMADAS, anio, mes), nombres)
        if _incluir(f)]
    ventas = [f for f in _filtrar_owner(
        _llamadas_campanas_y_colas(DATATYPE_CALLS_WITH_SCRIPT, TEMPLATE_VENTAS, anio, mes), nombres)
        if _incluir(f) and _es_venta(f)]

    por_agente = defaultdict(list)
    for f in llamadas:
        por_agente[_v(f, "agente") or "(sin agente)"].append(f)
    ventas_agente = defaultdict(list)
    for f in ventas:
        ventas_agente[_v(f, "agente") or "(sin agente)"].append(f)

    detalle = []
    for agente in sorted(set(por_agente) | set(ventas_agente)):
        ll = por_agente.get(agente, [])
        contestadas = [_a_int(_v(f, "talk_time")) for f in ll if _contestada(f)]
        vv = ventas_agente.get(agente, [])
        detalle.append({
            "agente": agente,
            "llamadas": len(ll),
            "en_campanas": sum(1 for f in ll if f.get("_tipo") == "campaña"),
            "en_colas": sum(1 for f in ll if f.get("_tipo") == "cola"),
            "contestadas": len(contestadas),
            "minutos_conversacion": round(sum(contestadas) / 60, 1),
            "ventas": len(vv),
            "pct_ventas_sobre_contestadas": round(100 * len(vv) / len(contestadas), 1) if contestadas else None,
            "ventas_por_producto": dict(Counter(_v(f, "final") for f in vv).most_common()),
            "outcomes": dict(Counter(_v(f, "outcome_llamada") for f in ll).most_common()),
        })
    return {
        "periodo": f"{anio}-{mes:02d}",
        "total_llamadas": len(llamadas),
        "total_ventas": len(ventas),
        "agentes": sorted(detalle, key=lambda d: -d["ventas"]),
    }


@mcp.tool()
def llamadas_servicio(
    anio: int,
    mes: int,
    incluir_no_servicio: bool = False,
    nombres: list[str] | None = None,
) -> dict:
    """Todas las llamadas del mes de campañas y colas clasificadas como "servicio",
    separadas en dos bloques: campañas (salientes) y colas (entrantes).

    La clasificación se hace por el nombre de la campaña/cola con las reglas
    SERVICIO_EMPIEZA_POR y SERVICIO_EXACTO (sin distinguir mayúsculas), así que
    las campañas o colas nuevas que cumplan las reglas entran automáticamente.

    incluir_no_servicio: si es True, añade también el resumen de lo que NO es servicio.
    nombres: opcional, limita a campañas/colas cuyo nombre contenga alguno de estos textos.

    Por cada campaña/cola devuelve: llamadas totales, llamadas con conversación,
    minutos de conversación, duración media (seg) y desglose por outcome.
    """
    resultado = {"periodo": f"{anio}-{mes:02d}"}
    for owner_type, etiqueta in (("campaign", "campanas"), ("queue", "colas")):
        filas = _generar_y_descargar(owner_type, DATATYPE_ALL_CALLS, TEMPLATE_LLAMADAS, anio, mes)
        filas = _filtrar_owner(filas, nombres)

        bloques = {"servicio": defaultdict(list)}
        if incluir_no_servicio:
            bloques["no_servicio"] = defaultdict(list)
        for f in filas:
            nombre = _owner(f)
            clave = "servicio" if _es_servicio(nombre) else "no_servicio"
            if clave in bloques:
                bloques[clave][nombre].append(f)

        resultado[etiqueta] = {}
        for clave, por_owner in bloques.items():
            detalle = []
            for nombre, llamadas in sorted(por_owner.items()):
                con_conv = [_a_int(_v(f, "talk_time")) for f in llamadas if _contestada(f)]
                detalle.append({
                    "nombre": nombre,
                    "llamadas": len(llamadas),
                    "contestadas": len(con_conv),
                    "minutos_conversacion": round(sum(con_conv) / 60, 1),
                    "duracion_media_seg": round(sum(con_conv) / len(con_conv)) if con_conv else 0,
                    "outcomes": dict(Counter(_v(f, "outcome_llamada") for f in llamadas).most_common()),
                })
            resultado[etiqueta][clave] = {
                "total_llamadas": sum(d["llamadas"] for d in detalle),
                "detalle": detalle,
            }
    return resultado


@mcp.tool()
def columnas_informe(tipo: str, anio: int, mes: int) -> dict:
    """Utilidad de puesta en marcha.
    tipo = 'leads', 'ventas', 'ventas_colas', 'llamadas_campanas' o 'llamadas_colas'.
    Devuelve solo las cabeceras del CSV y el número de filas (ningún dato de contactos),
    para comprobar los nombres de columna del diccionario COL.
    """
    opciones = {
        "leads": ("campaign", DATATYPE_CONTACT_STATE_BY_LOAD_DATE, TEMPLATE_LEADS),
        "ventas": ("campaign", DATATYPE_CALLS_WITH_SCRIPT, TEMPLATE_VENTAS),
        "ventas_colas": ("queue", DATATYPE_CALLS_WITH_SCRIPT, TEMPLATE_VENTAS),
        "llamadas_campanas": ("campaign", DATATYPE_ALL_CALLS, TEMPLATE_LLAMADAS),
        "llamadas_colas": ("queue", DATATYPE_ALL_CALLS, TEMPLATE_LLAMADAS),
    }
    if tipo not in opciones:
        raise ValueError(f"tipo debe ser uno de: {', '.join(opciones)}")
    owner_type, data_type, template_id = opciones[tipo]
    filas = _generar_y_descargar(owner_type, data_type, template_id, anio, mes)
    return {"columnas": list(filas[0].keys()) if filas else [], "filas": len(filas)}


if __name__ == "__main__":
    # MCP_TRANSPORT=stdio  -> uso local en Claude Desktop (sin servidor ni OAuth)
    # MCP_TRANSPORT=streamable-http (por defecto) -> servidor remoto para claude.ai
    mcp.run(transport=os.environ.get("MCP_TRANSPORT", "streamable-http"))
