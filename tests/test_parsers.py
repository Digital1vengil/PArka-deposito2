from app.parsers import extraer_id, parse_zpl, leer_archivo, autodetectar

ZPL = """^XA
^FO10,10^BCN,120,N,N^FD>:47383333425^FS
^FO10,200^FDVenta: 20000^FS^FO100,200^FD12345678901^FS
^FO10,300^FB600,2,0,L^FH^FDCampera Parka Negra Talle M^FS
^FO10,400^FDSKU: PK-001-M^FS
^FO10,500^FH^FDJuan P_C3_A9rez (JUANPE123)^FS
^XZ
^XA
^FO10,10^BQN,2,5^FDLA,{"id":"47383333999","t":"lm"}^FS
^XZ"""


def test_extraer_id():
    assert extraer_id('{"id":"47383333425","t":"lm"}') == "47383333425"
    assert extraer_id('LA,{"id":"47383333425","t":"lm"}') == "47383333425"
    assert extraer_id("]C047383333425") == "47383333425"
    assert extraer_id(">:47383333425") == "47383333425"
    assert extraer_id("  47383333425 ") == "47383333425"
    assert extraer_id("#1234") == "1234"


def test_parse_zpl():
    paqs = parse_zpl(ZPL)
    assert len(paqs) == 2
    p = paqs[0]
    assert p["tracking"] == "47383333425"
    assert p["venta_id"] == "2000012345678901"
    assert p["sku"] == "PK-001-M"
    assert "Campera" in p["producto"]
    assert p["comprador"] == "Juan Pérez"
    assert paqs[1]["tracking"] == "47383333999"


def test_leer_archivo_zpl():
    r = leer_archivo("etiquetas.txt", ZPL.encode())
    assert r["formato"] == "zpl" and len(r["paquetes"]) == 2


def test_csv_tiendanube_agrupa_por_orden():
    csv = ("Número de orden;Nombre del comprador;Nombre del producto;SKU;Cantidad del producto\n"
           "1001;Ana;Remera;R-1;1\n1001;Ana;Buzo;B-2;2\n1002;Luis;Campera;C-3;1\n").encode("utf-8")
    r = leer_archivo("ventas.csv", csv, tipo="tiendanube")
    assert r["formato"] == "tabla"
    assert len(r["paquetes"]) == 2
    a = [p for p in r["paquetes"] if p["tracking"] == "1001"][0]
    assert a["cantidad"] == 3 and "Buzo" in a["producto"] and "B-2" in a["sku"]


def test_csv_sin_columna_tracking_pide_mapeo():
    csv = "Cliente,Producto,Codigo raro\nAna,Remera,999111\n".encode()
    r = leer_archivo("x.csv", csv, tipo="flex")
    assert r["paquetes"] == []
    r2 = leer_archivo("x.csv", csv, tipo="flex", mapeo={"tracking": "Codigo raro"})
    assert r2["paquetes"][0]["tracking"] == "999111"


def test_autodetect_ml():
    h = ["# de venta", "Comprador", "Título de la publicación", "Número de envío", "SKU", "Unidades"]
    m = autodetectar(h)
    assert m["tracking"] == "Número de envío"
    assert m["venta_id"] == "# de venta"
    assert m["cantidad"] == "Unidades"
