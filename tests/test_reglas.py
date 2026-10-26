"""Tests de las reglas de negocio (no llaman a GoContact)."""
import os
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
os.environ.setdefault("GOCONTACT_BASE", "https://ejemplo.gocontact.es/poll")
os.environ.setdefault("GOCONTACT_USER", "test@test")
os.environ.setdefault("GOCONTACT_PASS", "test")
for v in ("GC_TEMPLATE_LEADS", "GC_TEMPLATE_VENTAS", "GC_TEMPLATE_LLAMADAS"):
    os.environ.setdefault(v, "1")
os.environ.setdefault("GC_CONFIG", str(RAIZ / "config" / "business_rules.example.json"))
sys.path.insert(0, str(RAIZ))

import server as s  # noqa: E402


def fila(**campos):
    """Crea una fila de CSV usando las claves internas de COL."""
    return {s.COL[k]: v for k, v in campos.items()}


def test_proveedor_manda_el_id_formulario():
    assert s._proveedor("form-afiliadoa-salud", "google") == "Afiliado A"


def test_proveedor_usa_el_campo_solo_sin_id_formulario():
    assert s._proveedor("", "google") == "Google Ads"
    assert s._proveedor("", "") == "(sin id formulario)"


def test_id_formulario_desconocido_es_propio():
    assert s._proveedor("landing-propia", "") == "Otros / propios"


def test_venta_exige_grupo_producto_y_gestion():
    ok = fila(grupo="Venta", final="Salud", gestion="Contrato grabado telefónicamente")
    sin_gestion = fila(grupo="Venta", final="Salud", gestion="Información")
    assert s._es_venta(ok)
    assert not s._es_venta(sin_gestion)


def test_no_contactado_con_tiempo_de_conversacion_no_cuenta_como_contestada():
    assert not s._contestada(fila(outcome_llamada="No Contactado", talk_time="35"))
    assert s._contestada(fila(outcome_llamada="Interesado", talk_time="35"))
    assert not s._contestada(fila(outcome_llamada="Interesado", talk_time="0"))


def test_fechas_se_normalizan():
    assert s._fecha("11072026") == "11/07/2026"
    assert s._fecha("9-7-2026") == "09/07/2026"
    assert s._a_iso("03/09/2026 10:00") == "2026-09-03"


def test_clasificacion_servicio_sin_mayusculas():
    assert s._es_servicio("servicio | renovaciones")
    assert s._es_servicio("CALLBACKS")
    assert not s._es_servicio("Captación | Salud")


def test_rango_mes_respeta_limite_de_3_meses():
    inicio, fin = s._rango_mes(2026, 11, meses_extra=5)
    assert inicio == "2026-11-01 00:00:00"
    assert fin == "2027-01-31 24:00:00"
