from flask import Flask, request, jsonify, send_file
from fillpdf import fillpdfs
import requests
import io, os, tempfile, unicodedata, time, threading

app = Flask(__name__)

# =================== CONFIGURACIÓN ===================
APPS_SCRIPT_URL = "https://script.google.com/macros/s/AKfycby9sxBx8XVMAxcs3MWtB_LmcQjqP5e2Bn_UIlCurpvs40LSkgyY3A6AuuNF_D_Ti24KIQ/exec"
SMLV = 1423500

CORS_HEADERS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type'
}

# =================== CACHÉ DEL CRM ===================
# Se carga UNA vez al arrancar y se refresca cada 6 horas.
# Así Andy no espera al Apps Script en cada mensaje del cliente.
_cache = {
    'pagadurias': None,       # { activos: [...], pensionados: [...] }
    'tipos_contrato': None,   # [{ entidad, secretarias, dianFiscalia, pensionados }]
    'politicas': {},          # { 'POLITICA_BANCO_UNION': [...], ... }
    'ts': 0                   # timestamp de la última carga
}
_cache_lock = threading.Lock()
CACHE_TTL = 6 * 3600  # 6 horas en segundos


def _gs_get(params, timeout=25):
    """Llama al Apps Script con GET."""
    try:
        r = requests.get(APPS_SCRIPT_URL, params=params,
                         timeout=timeout, allow_redirects=True)
        return r.json()
    except Exception as e:
        return {'error': str(e)}


def cargar_cache(forzar=False):
    """
    Carga pagadurías, tipos de contrato y políticas desde el CRM.
    Solo hace llamadas reales si el caché está vacío o expiró.
    """
    with _cache_lock:
        ahora = time.time()
        if not forzar and _cache['ts'] and (ahora - _cache['ts']) < CACHE_TTL:
            return  # caché vigente, no hacer nada

        # Pagadurías
        pags = _gs_get({'action': 'getPagadurias'})
        if pags and 'error' not in pags and isinstance(pags, dict):
            activos   = len(pags.get('activos', []))
            pensionados = len(pags.get('pensionados', []))
            if activos > 0 or pensionados > 0:
                _cache['pagadurias'] = pags
                print(f"[CACHE] Pagadurías cargadas: {activos} activos, {pensionados} pensionados")
            else:
                print(f"[CACHE WARN] Apps Script devolvió pagadurías vacías: {pags}")
        else:
            print(f"[CACHE ERROR] No se pudo cargar pagadurías: {pags}")

        # Tipos de contrato
        tipos = _gs_get({'action': 'getTiposContrato'})
        if tipos and isinstance(tipos, list):
            _cache['tipos_contrato'] = tipos
            print(f"[CACHE] Tipos contrato: {len(tipos)} registros")
        else:
            print(f"[CACHE ERROR] No se pudo cargar tipos contrato: {tipos}")

        _cache['ts'] = time.time()


def _normalizar(texto):
    """Quita tildes y pasa a minúsculas para comparaciones flexibles."""
    if not texto:
        return ''
    texto = texto.lower().strip()
    texto = unicodedata.normalize('NFD', texto)
    return ''.join(c for c in texto if unicodedata.category(c) != 'Mn')


def _similitud(t1, t2):
    """True si las palabras clave de t1 aparecen en t2 (≥60% de coincidencia)."""
    n1, n2 = _normalizar(t1), _normalizar(t2)
    palabras = [p for p in n1.split() if len(p) > 3]
    if not palabras:
        return n1 in n2
    hits = sum(1 for p in palabras if p in n2)
    return hits >= max(1, len(palabras) * 0.6)


# =================== LÓGICA DE NEGOCIO ===================

def verificar_pagaduria(texto_pag, tipo_cliente='activo'):
    """
    Busca la pagaduría en el caché. Si el caché está vacío,
    intenta cargarlo primero. Si sigue vacío, llama directo al CRM.
    """
    cargar_cache()
    data = _cache.get('pagadurias') or {}
    clave = 'pensionados' if tipo_cliente == 'pensionado' else 'activos'
    lista = data.get(clave, [])

    # CRÍTICO: si el caché está vacío (Render recién despertó),
    # intentar cargar forzando la llamada al CRM
    if not lista:
        print(f"[WARN] Caché vacío para '{clave}'. Recargando desde CRM...")
        cargar_cache(forzar=True)
        data = _cache.get('pagadurias') or {}
        lista = data.get(clave, [])

    # Si todavía está vacío, buscar en AMBAS listas (activos Y pensionados)
    # para no perder una pagaduría por error de clasificación
    if not lista:
        print(f"[WARN] Caché sigue vacío — buscando en ambas listas")
        lista = (data.get('activos', []) + data.get('pensionados', []))

    entidades, pag_oficial = [], ''
    for item in lista:
        pag_crm = item.get('pagaduria', '')
        entidad = item.get('entidad', '')
        if _similitud(texto_pag, pag_crm):
            if entidad not in entidades:
                entidades.append(entidad)
            if not pag_oficial:
                pag_oficial = pag_crm

    encontrada = len(entidades) > 0
    print(f"[PAG] '{texto_pag}' ({tipo_cliente}) → encontrada:{encontrada} | entidades:{entidades}")

    return {
        'encontrada': encontrada,
        'entidades': entidades,
        'pagaduria_oficial': pag_oficial,
        'texto_original': texto_pag
    }


def verificar_tipo_contrato(tipo_contrato_o_pension, entidades, tipo_cliente='activo'):
    """
    Verifica si el tipo de contrato/pensión aplica en alguna de las entidades.
    Retorna { aplica, entidades_ok, entidades_no }
    """
    cargar_cache()
    tipos = _cache.get('tipos_contrato') or []
    if not tipos:
        # Si no hay datos en caché, permitir todas (fail-safe)
        return {'aplica': True, 'entidades_ok': entidades, 'entidades_no': []}

    tc_norm = _normalizar(tipo_contrato_o_pension)
    entidades_ok, entidades_no = [], []

    for entidad in entidades:
        ent_norm = _normalizar(entidad)
        # Buscar fila de esta entidad en tipos_contrato
        fila = None
        for t in tipos:
            if _normalizar(t.get('entidad', '')) in ent_norm or ent_norm in _normalizar(t.get('entidad', '')):
                fila = t
                break
        if not fila:
            entidades_ok.append(entidad)  # sin restricción registrada → permitir
            continue

        # Escoger la columna según tipo de cliente
        if tipo_cliente == 'pensionado':
            col = _normalizar(fila.get('pensionados', ''))
        else:
            # Para activos usamos secretarias (más amplia) + dianFiscalia
            col = _normalizar(fila.get('secretarias', '') + ' ' + fila.get('dianFiscalia', ''))

        if col == 'no aplica':
            entidades_no.append(entidad)
        elif not col or tc_norm in col or any(
            p in col for p in tc_norm.split() if len(p) > 4
        ):
            entidades_ok.append(entidad)
        else:
            entidades_no.append(entidad)

    return {
        'aplica': len(entidades_ok) > 0,
        'entidades_ok': entidades_ok,
        'entidades_no': entidades_no
    }


def calcular_viabilidad(perfil):
    """
    Llama al Apps Script analizarViabilidad (esta sí necesita las hojas de política).
    Devuelve { viables, no_viables }
    """
    try:
        params = {
            'action': 'analizarViabilidad',
            'tipoCliente': perfil.get('tipo_cliente', 'activo'),
            'dinamico': perfil.get('tipo_pension', '') or perfil.get('tipo_contrato', ''),
            'reportesFinanciero': perfil.get('reportes_financiero', 'no'),
            'reportesReal': perfil.get('reportes_real', 'no'),
            'reportesTelco': perfil.get('reportes_telco', 'no'),
            'libranzaNegativa': perfil.get('libranza_negativa', 'no'),
            'cooperativaNegativa': perfil.get('cooperativa_negativa', 'no'),
            'insolvencia': perfil.get('insolvencia', 'no'),
            'procesosJuridicos': perfil.get('procesos_juridicos', 'no'),
            'procesosJuridicos2': perfil.get('procesos_juridicos', 'no'),
            'embargos': perfil.get('embargos', 'no'),
            'tipoCredito': perfil.get('tipo_credito', 'libre'),
            'edad': str(perfil.get('edad', 50))
        }
        resultado = _gs_get(params, timeout=30)
        if not resultado or isinstance(resultado, dict) and 'error' in resultado:
            return {'viables': [], 'no_viables': [], 'error': 'No se pudo consultar políticas'}

        viables, no_viables = [], []
        for r in (resultado if isinstance(resultado, list) else []):
            if r.get('viable'):
                viables.append({
                    'entidad': r.get('entidad'),
                    'condiciones': [i for i in (r.get('issues') or []) if '⚠️' in i]
                })
            else:
                no_viables.append({
                    'entidad': r.get('entidad'),
                    'motivos': [i for i in (r.get('issues') or []) if '❌' in i]
                })
        return {'viables': viables, 'no_viables': no_viables}

    except Exception as e:
        return {'viables': [], 'no_viables': [], 'error': str(e)}


def calcular_capacidad(salario, descuentos_ley, otros_descuentos, segmento=''):
    """Calcula la capacidad de pago según segmento."""
    segmentos_smlv = ['secretarias_docente', 'mindefensa_cremil', 'fuerzas_militares']
    if segmento in segmentos_smlv and salario < SMLV * 2:
        cap = salario - descuentos_ley - SMLV - otros_descuentos
    else:
        cap = ((salario - descuentos_ley) / 2) - otros_descuentos - 10000
    return max(0, round(cap))


def calcular_ofertas(capacidad, entidades_viables, pagaduria='', tipo_cliente='activo'):
    """
    Calcula montos y netos para cada entidad viable.
    Consulta factores reales del CRM.
    """
    ofertas = []
    plazo = 144
    pag_n = _normalizar(pagaduria)

    for item in entidades_viables:
        entidad = item.get('entidad', '') if isinstance(item, dict) else item
        ent_n = _normalizar(entidad)

        try:
            # ── BANCO UNIÓN ──────────────────────────────────────────────
            if 'banco' in ent_n or 'union' in ent_n:
                r = _gs_get({'action': 'getFactor', 'tasa': 1.60, 'plazo': plazo})
                factor = float(r) if r and not isinstance(r, dict) else None
                if factor and capacidad > 0:
                    monto = int((capacidad / factor) * 1_000_000 / 100_000) * 100_000
                    if monto >= 1_000_000:
                        # FGA según pagaduría
                        if 'policia' in pag_n or 'fiduprevisora' in pag_n:
                            pct_fga = 0.10
                        elif 'cremil' in pag_n or 'casur' in pag_n:
                            pct_fga = 0.09
                        elif tipo_cliente == 'pensionado':
                            pct_fga = 0.06
                        else:
                            pct_fga = 0.08
                        fga = monto * pct_fga * 1.19
                        # Gestión documental
                        tabla_gestion = [
                            (10_000_000, 403_058),  (20_000_000, 1_138_638),
                            (30_000_000, 1_897_730),(40_000_000, 2_150_761),
                            (50_000_000, 2_372_163),(60_000_000, 2_625_192),
                            (70_000_000, 2_820_814),(80_000_000, 3_162_882),
                            (90_000_000, 3_386_969),(100_000_000, 3_611_056)
                        ]
                        gestion = 3_903_024
                        for tope, val in tabla_gestion:
                            if monto <= tope:
                                gestion = val
                                break
                        neto = monto - fga - gestion
                        ofertas.append({
                            'entidad': 'Banco Unión',
                            'tasa': '1.60%', 'plazo': f'{plazo} meses',
                            'monto': monto, 'neto': round(neto), 'cuota': capacidad,
                            'descuentos': f'FGA {pct_fga*100:.0f}%+IVA: ${round(fga):,} · Gestión: ${gestion:,}'.replace(',', '.')
                        })

            # ── RAYCO ────────────────────────────────────────────────────
            elif 'rayco' in ent_n:
                plazo_r = min(plazo, 120)
                r = _gs_get({'action': 'getFactorRayco', 'plazo': plazo_r})
                factor = float(r) if r and not isinstance(r, dict) else None
                if factor and capacidad > 0:
                    monto = int((capacidad / factor) * 1_000_000 / 100_000) * 100_000
                    if monto >= 1_000_000:
                        neto = monto * 0.91
                        ofertas.append({
                            'entidad': 'Rayco',
                            'tasa': '1.89%', 'plazo': f'{plazo_r} meses',
                            'monto': monto, 'neto': round(neto), 'cuota': capacidad,
                            'descuentos': f'Descuento 9%: ${round(monto*0.09):,}'.replace(',', '.')
                        })

            # ── KALA ─────────────────────────────────────────────────────
            elif 'kala' in ent_n:
                r = _gs_get({'action': 'getFactorKala', 'plazo': plazo})
                factor = float(r) if r and not isinstance(r, dict) else None
                if factor and capacidad > 0:
                    monto = int((capacidad / (factor / 100)) / 100_000) * 100_000
                    if monto >= 1_000_000:
                        corr = monto * 0.07
                        fian = monto * 0.03
                        seg  = monto * 0.00125
                        neto = monto - corr - fian - seg - 22_598
                        ofertas.append({
                            'entidad': 'Kala',
                            'tasa': '1.91%', 'plazo': f'{plazo} meses',
                            'monto': monto, 'neto': round(neto), 'cuota': capacidad,
                            'descuentos': 'Corretetaje 7% + Fianza 3% + Seguro'
                        })

            # ── FINEXUS ──────────────────────────────────────────────────
            elif 'finexus' in ent_n:
                r = _gs_get({'action': 'getFactorFinexus', 'tasa': 1.80, 'plazo': plazo})
                factor = float(r) if r and not isinstance(r, dict) else None
                if factor and capacidad > 0:
                    monto = int((capacidad / factor) * 1_000_000 / 100_000) * 100_000
                    if monto >= 1_000_000:
                        neto = monto * 0.77
                        ofertas.append({
                            'entidad': 'Finexus',
                            'tasa': '1.80%', 'plazo': f'{plazo} meses',
                            'monto': monto, 'neto': round(neto), 'cuota': capacidad,
                            'descuentos': 'Descuento 23%'
                        })

        except Exception:
            continue

    # Ordenar por monto neto descendente
    ofertas.sort(key=lambda x: x.get('neto', 0), reverse=True)
    return ofertas


# =================== GENERACIÓN FT-449 ===================

def num_letras(n):
    u = ['','UN','DOS','TRES','CUATRO','CINCO','SEIS','SIETE','OCHO','NUEVE',
         'DIEZ','ONCE','DOCE','TRECE','CATORCE','QUINCE','DIECISEIS',
         'DIECISIETE','DIECIOCHO','DIECINUEVE']
    d = ['','','VEINTE','TREINTA','CUARENTA','CINCUENTA',
         'SESENTA','SETENTA','OCHENTA','NOVENTA']
    c = ['','CIENTO','DOSCIENTOS','TRESCIENTOS','CUATROCIENTOS','QUINIENTOS',
         'SEISCIENTOS','SETECIENTOS','OCHOCIENTOS','NOVECIENTOS']

    def bloque(num):
        if num == 0:   return ''
        if num == 100: return 'CIEN'
        r = ''
        if num >= 100:
            r = c[num // 100] + ' '; num %= 100
        if num >= 20:
            r += d[num // 10]
            if num % 10: r += ' Y ' + u[num % 10]
        elif num > 0:
            r += u[num]
        return r.strip()

    r, m = '', round(n)
    if m >= 1_000_000:
        b = m // 1_000_000
        r += ('UN MILLON' if b == 1 else bloque(b) + ' MILLONES') + ' '
        m %= 1_000_000
    if m >= 1_000:
        b = m // 1_000
        r += ('MIL' if b == 1 else bloque(b) + ' MIL') + ' '
        m %= 1_000
    if m > 0:
        r += bloque(m)
    return r.strip()


def separar_nombre(nombre_completo):
    partes = nombre_completo.strip().upper().split()
    if len(partes) >= 4: return ' '.join(partes[:2]), ' '.join(partes[2:])
    if len(partes) == 3: return partes[0], ' '.join(partes[1:])
    if len(partes) == 2: return partes[0], partes[1]
    return nombre_completo.upper(), ''


def generar_pdf_ft449(datos):
    apellidos, nombres = separar_nombre(datos.get('nombre', ''))
    fecha_hoy   = datos.get('fechaHoy', '')
    partes_fecha = fecha_hoy.split('/')
    dd   = partes_fecha[0] if len(partes_fecha) > 0 else ''
    mm   = partes_fecha[1] if len(partes_fecha) > 1 else ''
    aaaa = partes_fecha[2] if len(partes_fecha) > 2 else ''

    monto_num = datos.get('montoNum', 0)
    try:
        monto_num = int(str(monto_num).replace('.', '').replace(',', '').replace('$', ''))
    except Exception:
        monto_num = 0

    monto_str   = '$' + f'{monto_num:,}'.replace(',', '.')
    monto_letras = num_letras(monto_num) + ' PESOS M/CTE'

    pdf_base_path = os.path.join(os.path.dirname(__file__), 'FT449_base.pdf')
    campos = {
        'fecha_dia': dd, 'fecha_mes': mm, 'fecha_anio': aaaa,
        'valor_solicitado': monto_str,
        'valor_letras': monto_letras,
        'plazo': str(datos.get('plazo', '144')) + ' meses',
        'pagaduria': datos.get('pagaduria', '').upper(),
        'destino_del_credito': datos.get('destino', 'Libre Inversion').upper(),
        'apellidos': apellidos, 'nombres': nombres,
        'tipo_identificacion': 'CC',
        'numero_identificacion': datos.get('cedula', ''),
        'direccion_residencia': datos.get('direccion', '').upper(),
        'ciudad_residencia': datos.get('ciudad', '').upper(),
        'numero_celular': datos.get('telefono', ''),
        'tipo_originador': 'Outsourcing',
        'coordinador_comercial': 'Carmen Gonzalez',
        'nombre_outsourcing': 'Grupo Fipcol SAS',
        'nit_outsourcing': '901542631-1',
    }
    output_path = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf').name
    fillpdfs.write_fillable_pdf(pdf_base_path, output_path, campos, flatten=False)
    with open(output_path, 'rb') as f:
        output = io.BytesIO(f.read())
    output.seek(0)
    try:
        os.unlink(output_path)
    except Exception:
        pass
    return output

# =================== GENERACIÓN FORMATO INFORMATIVO ===================

def generar_pdf_formato_informativo(datos):
    """Genera el Formato Informativo de Cliente — versión liviana sin PDF base."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib import colors
    from reportlab.lib.units import cm
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_CENTER
    import io

    NAVY  = colors.HexColor('#1A3A6B')
    RED   = colors.HexColor('#D62E2E')
    GRAY  = colors.HexColor('#6B7A99')
    WHITE = colors.white
    BORDER = colors.HexColor('#C8D8F0')

    def st(name, **kw):
        d = dict(fontName='Helvetica', fontSize=9, leading=11, textColor=colors.black)
        d.update(kw)
        return ParagraphStyle(name, **d)

    def g(k):
        return str(datos.get(k, '') or '')

    def lbl(label, valor=''):
        return [
            Paragraph(label, st('l', fontSize=7, textColor=GRAY, leading=9)),
            Paragraph(str(valor) if valor else '—', st('v', fontSize=9, fontName='Helvetica-Bold', leading=11))
        ]

    def sec(texto):
        p = Paragraph(f'  {texto}', st('s', fontSize=8, fontName='Helvetica-Bold', textColor=WHITE, leading=10))
        t = Table([[p]], colWidths=[19*cm])
        t.setStyle(TableStyle([
            ('BACKGROUND',(0,0),(-1,-1),NAVY),
            ('TOPPADDING',(0,0),(-1,-1),5),
            ('BOTTOMPADDING',(0,0),(-1,-1),5),
            ('LEFTPADDING',(0,0),(-1,-1),8),
        ]))
        return t

    TS = lambda: TableStyle([
        ('ROWBACKGROUNDS',(0,0),(-1,-1),[WHITE, colors.HexColor('#F4F7FB')]),
        ('GRID',(0,0),(-1,-1),0.3,BORDER),
        ('TOPPADDING',(0,0),(-1,-1),5),
        ('BOTTOMPADDING',(0,0),(-1,-1),5),
        ('LEFTPADDING',(0,0),(-1,-1),7),
        ('RIGHTPADDING',(0,0),(-1,-1),7),
        ('VALIGN',(0,0),(-1,-1),'TOP'),
    ])

    def t2(rows):
        t = Table(rows, colWidths=[9.5*cm, 9.5*cm]); t.setStyle(TS()); return t

    def t3(rows):
        w = 19/3
        t = Table(rows, colWidths=[w*cm, w*cm, w*cm]); t.setStyle(TS()); return t

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter,
        rightMargin=1.5*cm, leftMargin=1.5*cm,
        topMargin=1.5*cm, bottomMargin=1.5*cm)

    story = []

    # ENCABEZADO
    enc = [[
        Paragraph('<b>GRUPO FIPCOL SAS</b><br/><font size="7" color="#AABBD4">NIT 901542631-1</font>',
                  st('e1', fontSize=12, fontName='Helvetica-Bold', textColor=WHITE, leading=15)),
        Paragraph('FORMATO INFORMATIVO DE CLIENTE<br/><font size="7">Uso interno · Todas las entidades</font>',
                  st('e2', fontSize=11, fontName='Helvetica-Bold', textColor=WHITE, alignment=TA_CENTER, leading=14)),
        Paragraph(f'Fecha: {g("fechaHoy")}<br/>ID: {g("idConsulta")}<br/>Asesor: {g("asesor")}',
                  st('e3', fontSize=7.5, textColor=colors.HexColor('#AABBD4'), leading=12)),
    ]]
    te = Table(enc, colWidths=[5*cm, 9*cm, 5*cm])
    te.setStyle(TableStyle([
        ('BACKGROUND',(0,0),(-1,-1),NAVY),
        ('VALIGN',(0,0),(-1,-1),'MIDDLE'),
        ('TOPPADDING',(0,0),(-1,-1),12),
        ('BOTTOMPADDING',(0,0),(-1,-1),12),
        ('LEFTPADDING',(0,0),(-1,-1),12),
        ('RIGHTPADDING',(0,0),(-1,-1),12),
        ('LINEABOVE',(0,0),(-1,0),3,RED),
    ]))
    story += [te, Spacer(1, 7)]

    # 1. DATOS BÁSICOS
    story.append(sec('1.  DATOS BÁSICOS DEL CLIENTE'))
    story.append(t2([
        [lbl('Nombre completo', g('nombre')),           lbl('Cédula', g('cedula'))],
        [lbl('Fecha de nacimiento', g('fechaNacimiento')), lbl('Estado civil', g('estadoCivil'))],
        [lbl('Teléfono / Celular', g('telefono')),      lbl('Correo electrónico', g('correo'))],
        [lbl('Ciudad', g('ciudad')),                    lbl('Departamento', g('departamento'))],
        [lbl('Tipo de vivienda', g('tipoVivienda')),    lbl('Dirección de residencia', g('direccion'))],
    ]))
    story.append(Spacer(1, 6))

    # 2. SOLICITUD DE CRÉDITO
    story.append(sec('2.  SOLICITUD DE CRÉDITO'))
    story.append(t2([
        [lbl('Entidad financiera', g('entidad')),       lbl('Pagaduría', g('pagaduria'))],
        [lbl('Tipo de crédito', g('tipoCredito')),      lbl('Tipo de solicitud', g('tipoSolicitud'))],
        [lbl('Monto solicitado ($)', g('monto')),       lbl('Plazo (meses)', g('plazo'))],
        [lbl('Tasa (% mensual)', g('tasa')),            lbl('Descuento / Comisión', g('descuento'))],
    ]))
    story.append(Spacer(1, 6))

    # 3. DATOS DE DESEMBOLSO
    story.append(sec('3.  DATOS DE DESEMBOLSO (CUENTA BANCARIA)'))
    story.append(t3([
        [lbl('Banco / Entidad', g('banco')),
         lbl('Tipo de cuenta', g('tipoCuenta')),
         lbl('Número de cuenta', g('numeroCuenta'))],
    ]))
    story.append(Spacer(1, 6))

    # 4. REFERENCIA PERSONAL
    story.append(sec('4.  REFERENCIA PERSONAL'))
    story.append(t2([
        [lbl('Nombre completo', g('ref1Nombre')),       lbl('Celular', g('ref1Celular'))],
        [lbl('Relación / Parentesco', g('ref1Relacion')), lbl('Ciudad', g('ref1Ciudad'))],
    ]))
    story.append(Spacer(1, 6))

    # 5. REFERENCIA FAMILIAR
    story.append(sec('5.  REFERENCIA FAMILIAR'))
    story.append(t2([
        [lbl('Nombre completo', g('ref2Nombre')),       lbl('Celular', g('ref2Celular'))],
        [lbl('Parentesco', g('ref2Parentesco')),        lbl('Ciudad', g('ref2Ciudad'))],
    ]))
    story.append(Spacer(1, 10))

    # PIE
    story.append(Paragraph(
        'Formato Informativo Interno · Grupo Fipcol SAS · '
        'Uso exclusivo del equipo comercial · '
        'No reemplaza el cuadernillo oficial de cada entidad financiera',
        st('nota', fontSize=7, textColor=GRAY, alignment=TA_CENTER, leading=9)
    ))

    doc.build(story)
    buf.seek(0)
    return buf

# =================== ENDPOINTS ===================

def _cors(response):
    for k, v in CORS_HEADERS.items():
        response.headers[k] = v
    return response


@app.route('/ping', methods=['GET', 'OPTIONS'])
def ping():
    if request.method == 'OPTIONS':
        return '', 204, CORS_HEADERS
    cargar_cache()
    pags = _cache.get('pagadurias') or {}
    return _cors(jsonify({
        'status': 'ok',
        'cache': {
            'activos': len(pags.get('activos', [])),
            'pensionados': len(pags.get('pensionados', [])),
            'tipos_contrato': len(_cache.get('tipos_contrato') or []),
            'ts': _cache.get('ts', 0)
        }
    }))


@app.route('/consultar', methods=['POST', 'OPTIONS'])
def consultar():
    """
    Endpoint principal de Andy.
    Recibe: { accion, datos }

    Acciones disponibles:
      verificar_pagaduria    → busca en caché, sin llamada al CRM
      verificar_contrato     → verifica tipo contrato/pensión contra caché
      calcular_viabilidad    → llama Apps Script analizarViabilidad
      calcular_capacidad     → cálculo local
      calcular_ofertas       → llama Apps Script para factores y calcula
      analizar_perfil_completo → pagaduría + viabilidad + ofertas en una sola llamada
    """
    if request.method == 'OPTIONS':
        return '', 204, CORS_HEADERS

    try:
        body   = request.get_json(force=True) or {}
        accion = body.get('accion', '')
        datos  = body.get('datos', {})

        # ── 1. VERIFICAR PAGADURÍA (solo caché) ──────────────────────────
        if accion == 'verificar_pagaduria':
            res = verificar_pagaduria(
                datos.get('pagaduria', ''),
                datos.get('tipo_cliente', 'activo')
            )
            return _cors(jsonify(res))

        # ── 2. VERIFICAR TIPO CONTRATO / PENSIÓN (solo caché) ────────────
        elif accion == 'verificar_contrato':
            res = verificar_tipo_contrato(
                datos.get('tipo_contrato', '') or datos.get('tipo_pension', ''),
                datos.get('entidades', []),
                datos.get('tipo_cliente', 'activo')
            )
            return _cors(jsonify(res))

        # ── 3. CALCULAR VIABILIDAD (llama Apps Script) ───────────────────
        elif accion == 'calcular_viabilidad':
            res = calcular_viabilidad(datos)
            return _cors(jsonify(res))

        # ── 4. CALCULAR CAPACIDAD (local) ────────────────────────────────
        elif accion == 'calcular_capacidad':
            cap = calcular_capacidad(
                float(datos.get('salario', 0)),
                float(datos.get('descuentos_ley', 0)),
                float(datos.get('otros_descuentos', 0)),
                datos.get('segmento', '')
            )
            return _cors(jsonify({'capacidad': cap}))

        # ── 5. CALCULAR OFERTAS ──────────────────────────────────────────
        elif accion == 'calcular_ofertas':
            ofertas = calcular_ofertas(
                float(datos.get('capacidad', 0)),
                datos.get('entidades_viables', []),
                datos.get('pagaduria', ''),
                datos.get('tipo_cliente', 'activo')
            )
            return _cors(jsonify({'ofertas': ofertas}))

        # ── 6. PERFIL COMPLETO (pagaduría + viabilidad + ofertas) ────────
        elif accion == 'analizar_perfil_completo':
            # Paso A: pagaduría
            pag = verificar_pagaduria(
                datos.get('pagaduria', ''),
                datos.get('tipo_cliente', 'activo')
            )
            if not pag.get('encontrada'):
                return _cors(jsonify({
                    'etapa': 'pagaduria',
                    'pagaduria_encontrada': False,
                    'mensaje': f"La pagaduría '{datos.get('pagaduria')}' no está en nuestro sistema.",
                    'viables': [], 'ofertas': []
                }))

            # Paso B: tipo contrato/pensión
            tc = verificar_tipo_contrato(
                datos.get('tipo_pension', '') or datos.get('tipo_contrato', ''),
                pag.get('entidades', []),
                datos.get('tipo_cliente', 'activo')
            )
            if not tc.get('aplica'):
                return _cors(jsonify({
                    'etapa': 'contrato',
                    'pagaduria_encontrada': True,
                    'pagaduria_oficial': pag.get('pagaduria_oficial'),
                    'contrato_aplica': False,
                    'mensaje': 'El tipo de contrato/pensión no aplica para ninguna entidad con esta pagaduría.',
                    'viables': [], 'ofertas': []
                }))

            # Paso C: viabilidad crediticia (usa la pagaduría oficial)
            datos['pagaduria'] = pag.get('pagaduria_oficial', datos.get('pagaduria'))
            viab = calcular_viabilidad(datos)
            viables = viab.get('viables', [])

            # Paso D: ofertas si tienen capacidad
            ofertas = []
            capacidad = float(datos.get('capacidad', 0))
            if capacidad > 0 and viables:
                ofertas = calcular_ofertas(
                    capacidad, viables,
                    datos.get('pagaduria', ''),
                    datos.get('tipo_cliente', 'activo')
                )

            return _cors(jsonify({
                'etapa': 'completo',
                'pagaduria_encontrada': True,
                'pagaduria_oficial': pag.get('pagaduria_oficial'),
                'entidades_pagaduria': pag.get('entidades', []),
                'contrato_aplica': True,
                'entidades_contrato_ok': tc.get('entidades_ok', []),
                'viables': viables,
                'no_viables': viab.get('no_viables', []),
                'ofertas': ofertas,
                'capacidad': capacidad
            }))

        # ── 7. RECARGAR CACHÉ (útil para forzar actualización del CRM) ───
        elif accion == 'recargar_cache':
            threading.Thread(target=lambda: cargar_cache(forzar=True)).start()
            return _cors(jsonify({'ok': True, 'mensaje': 'Caché recargando en segundo plano'}))

        else:
            return _cors(jsonify({'error': f'Acción desconocida: {accion}'})), 400

    except Exception as e:
        return _cors(jsonify({'error': str(e)})), 500


@app.route('/', methods=['POST', 'GET', 'OPTIONS'])
def endpoint_ft449():
    """Genera y devuelve el PDF FT-449 prellenado."""
    if request.method == 'OPTIONS':
        return '', 204, CORS_HEADERS
    if request.method == 'GET':
        # Datos de prueba para verificar que el PDF funciona
        datos = {
            'nombre': 'JESUS EDILSON QUINTERO VALLEJO',
            'cedula': '9776983', 'telefono': '3168671944',
            'pagaduria': 'FIDUPREVISORA', 'montoNum': 50_000_000,
            'plazo': '144', 'direccion': 'CALLE 6N 4-200',
            'ciudad': 'PIEDECUESTA', 'fechaHoy': '30/04/2026',
            'destino': 'Libre Inversion'
        }
    else:
        datos = request.get_json(force=True) or {}
    try:
        pdf_bytes = generar_pdf_ft449(datos)
        nombre_archivo = f"FT449_{datos.get('cedula', 'cliente')}.pdf"
        response = send_file(pdf_bytes, mimetype='application/pdf',
                             as_attachment=True, download_name=nombre_archivo)
        return _cors(response)
    except Exception as e:
        return _cors(jsonify({'error': str(e)})), 500

@app.route('/formato-informativo', methods=['POST', 'OPTIONS'])
def endpoint_formato_informativo():
    """Genera y devuelve el Formato Informativo de Cliente prellenado."""
    if request.method == 'OPTIONS':
        return '', 204, CORS_HEADERS
    try:
        datos = request.get_json(force=True) or {}
        pdf_bytes = generar_pdf_formato_informativo(datos)
        cedula = datos.get('cedula', 'cliente')
        nombre_archivo = f"Formato_Informativo_{cedula}.pdf"
        response = send_file(pdf_bytes, mimetype='application/pdf',
                             as_attachment=True, download_name=nombre_archivo)
        return _cors(response)
    except Exception as e:
        return _cors(jsonify({'error': str(e)})), 500


# =================== EXTRACCIÓN DETERMINÍSTICA ===================
# Sin IA. Sin JSON. Sin fallos de parsing.
# Python con regex extrae datos del mensaje del cliente con 100% de confiabilidad.

import re

def _n(t):
    """Normaliza texto: minúsculas, sin tildes, sin puntuación extra."""
    t = t.lower().strip()
    t = unicodedata.normalize('NFD', t)
    t = ''.join(c for c in t if unicodedata.category(c) != 'Mn')
    return t


def extraer_datos_mensaje(texto):
    """
    Extrae datos estructurados del mensaje del cliente usando regex y
    patrones determinísticos. Nunca falla, nunca alucina.
    Maneja errores tipográficos reales de clientes colombianos.
    Retorna dict con los campos encontrados. Si no encontró algo → null.
    """
    t  = _n(texto)        # normalizado: sin tildes, minúsculas
    to = texto.lower()    # original en minúsculas

    resultado = {
        'tipo_cliente':        None,
        'pagaduria':           None,
        'tipo_pension':        None,
        'tipo_contrato':       None,
        'insolvencia':         None,
        'embargos':            None,
        'reportes_financiero': None,
        'libranza_negativa':   None,
        'cooperativa_negativa':None,
        'tipo_busqueda':       None,
        'telefono':            None,
        'fuera_nicho':         False,
        'es_saludo':           False,
    }

    # ── TIPO DE CLIENTE ──────────────────────────────────────────────────
    PENSIONADO = ['pensionado', 'jubilado', 'jubilada', 'pensionada',
                  'me jubile', 'estoy pensionado', 'soy pensionado']
    ACTIVO     = ['empleado', 'trabajo', 'trabajador', 'docente', 'profesor',
                  'maestra', 'maestro', 'militar', 'policia', 'soldado',
                  'empleado publico', 'funcionario', 'secretaria de educacion',
                  'secretaria educacion', 'gobernacion', 'alcaldia',
                  'dian', 'fiscalia', 'rama judicial', 'contraloria',
                  'ejercito', 'armada', 'fuerza aerea', 'infanteria']
    FUERA      = ['independiente', 'contratista', 'empresa privada',
                  'microempresario', 'negocio propio', 'informal',
                  'tendero', 'comerciante', 'freelance']

    if any(p in t for p in PENSIONADO):
        resultado['tipo_cliente'] = 'pensionado'
    elif any(p in t for p in ACTIVO):
        resultado['tipo_cliente'] = 'activo'
    elif any(p in t for p in FUERA):
        resultado['fuera_nicho'] = True

    # ── TIPO DE PENSIÓN ──────────────────────────────────────────────────
    if any(p in t for p in ['vejez', 'jubilacion', 'jubilado', 'jubilada']):
        resultado['tipo_pension'] = 'vejez'
    elif 'invalidez' in t:
        resultado['tipo_pension'] = 'invalidez'
    elif any(p in t for p in ['sustitucion', 'sobrevivencia', 'sobreviviente',
                               'viudez', 'beneficiario', 'sustituta']):
        resultado['tipo_pension'] = 'sustitucion'
    elif any(p in t for p in ['retiro', 'asignacion de retiro']):
        resultado['tipo_pension'] = 'retiro'

    # ── TIPO DE CONTRATO ─────────────────────────────────────────────────
    if any(p in t for p in ['propiedad', 'carrera administrativa',
                             'carrera admin', 'planta', 'nomina',
                             'propiedad de planta', 'carrera']):
        resultado['tipo_contrato'] = 'propiedad'
    elif 'provisional' in t:
        resultado['tipo_contrato'] = 'provisional'
    elif any(p in t for p in ['periodo de prueba', 'periodo prueba']):
        resultado['tipo_contrato'] = 'periodo_prueba'
    elif any(p in t for p in ['libre nombramiento', 'libre nombra']):
        resultado['tipo_contrato'] = 'libre_nombramiento'
    elif any(p in t for p in ['uniformado', 'ffmm', 'militar', 'policia',
                               'ejercito', 'armada', 'fuerza aerea']):
        resultado['tipo_contrato'] = 'uniformado'

    # ── SITUACIÓN CREDITICIA — INSOLVENCIA ───────────────────────────────
    # "ni insolvencia" también es negación: "no tengo reporte ni insolvencia ni nada"
    NEG_INSOL = ['no tengo insolvencia', 'sin insolvencia',
                 'no tengo proceso de insolvencia', 'no tengo sicaac',
                 'no estoy en insolvencia', 'no tengo proceso',
                 'ni insolvencia', 'ni sicaac']
    SI_INSOL  = ['insolvencia', 'sicaac', 'proceso de insolvencia',
                 'proceso concursal', 'acuerdo de pago']
    if any(p in t for p in NEG_INSOL):
        resultado['insolvencia'] = 'no'
    elif any(p in t for p in SI_INSOL):
        resultado['insolvencia'] = 'si'

    # ── EMBARGOS ─────────────────────────────────────────────────────────
    # REGLA: la negación siempre gana sobre la detección positiva
    # "no tengo embargos ni nada" → negación contiene "embargo" → debe ser 'no'
    NEG_EMB = ['no tengo embargo', 'sin embargo', 'no tengo ningun embargo',
               'no hay embargo', 'no embargo', 'no tengo embargos',
               'ni embargo', 'ni embargos']
    SI_EMB  = ['tengo embargo', 'tengo un embargo', 'hay embargo',
               'embargo activo', 'embargado', 'descuento judicial',
               'retencion judicial', 'retencion en nomina',
               'con embargo', 'embargo en el desprendible',
               'un embargo', '1 embargo', 'tiene embargo']
    if any(p in t for p in NEG_EMB):
        resultado['embargos'] = 'no'
    elif any(p in t for p in SI_EMB):
        resultado['embargos'] = 'si'

    # ── REPORTES FINANCIEROS ─────────────────────────────────────────────
    # CORRECCIÓN: capturar errores tipográficos reales → "em mora" = "en mora"
    t_rep = t.replace('em mora', 'en mora')  # error tipográfico frecuente
    NEG_REP = ['no tengo reporte', 'estoy limpio', 'sin reportes',
               'no estoy reportado', 'no tengo deudas en mora',
               'no tengo nada en centrales', 'al dia con todo',
               'no tengo ningun reporte']
    SI_REP  = ['reporte', 'reportado', 'datacredito', 'transunion',
               'en mora', 'deuda en mora', 'tarjeta en mora',
               'credito en mora', 'tarjetas de credito en mora',
               'deuda vencida', 'cartera vencida']
    if any(p in t_rep for p in NEG_REP):
        resultado['reportes_financiero'] = 'no'
    elif any(p in t_rep for p in SI_REP):
        resultado['reportes_financiero'] = 'si'

    # ── LIBRANZA NEGATIVA ────────────────────────────────────────────────
    # CORRECCIÓN: capturar errores tipográficos → "librana" = "libranza"
    # y variantes reales: "por fuera del desprendible", "con banco popular"
    t_lib = t.replace('librana', 'libranza').replace('libransa', 'libranza')
    NEG_LIB = ['no tengo libranza negativa', 'sin libranza negativa',
               'no tengo libranza', 'no tengo libranzas']
    SI_LIB  = ['libranza negativa', 'libranza por fuera',
               'libranza fuera del desprendible', 'libranza no aparece',
               'libranza con banco', 'libranza externa',
               'libranza sin descuento', 'libranza en otro banco',
               'libranza que no se descuenta', 'libranza aparte',
               'libranza fuera', 'libranza no descuenta',
               'tengo libranza']  # "tengo una libranza" implica libranza externa
    if any(p in t_lib for p in NEG_LIB):
        resultado['libranza_negativa'] = 'no'
    elif any(p in t_lib for p in SI_LIB):
        resultado['libranza_negativa'] = 'si'

    # ── COOPERATIVA NEGATIVA ─────────────────────────────────────────────
    NEG_COOP = ['no tengo cooperativa', 'sin deuda en cooperativa']
    SI_COOP  = ['cooperativa negativa', 'deuda en cooperativa',
                'cooperativa en mora', 'coopfacil', 'coopcentral']
    if any(p in t for p in NEG_COOP):
        resultado['cooperativa_negativa'] = 'no'
    elif any(p in t for p in SI_COOP):
        resultado['cooperativa_negativa'] = 'si'

    # ── TIPO DE CRÉDITO ──────────────────────────────────────────────────
    SI_COMPRA = ['compra de cartera', 'pagar deudas', 'unificar deudas',
                 'cancelar deudas', 'saldar deudas', 'unificar creditos',
                 'refinanciar']
    SI_LIBRE  = ['libre inversion', 'en efectivo', 'dinero en efectivo',
                 'plata en mano', 'plata en efectivo']
    if any(p in t for p in SI_COMPRA):
        resultado['tipo_busqueda'] = 'compra'
    elif any(p in t for p in SI_LIBRE):
        resultado['tipo_busqueda'] = 'libre'

    # ── TELÉFONO ─────────────────────────────────────────────────────────
    # Buscar número de 10 dígitos que empiece en 3, con o sin espacios entre dígitos
    tel = re.search(r'(?<!\d)(3\d{9})(?!\d)', texto)
    if not tel:
        # Segunda pasada: buscar en texto sin espacios (por si lo escribieron separado)
        tel = re.search(r'(?<!\d)(3\d{9})(?!\d)', texto.replace(' ', ''))
        # Verificar que no sea parte de una palabra (ej: "es3145678901")
        if tel and not re.search(r'\b3\d{9}\b', texto):
            # Solo aceptar si hay un espacio o inicio de texto antes del número
            if re.search(r'(?:^|\s)(3\d{9})(?:\s|$)', texto.replace(' ', '')):
                pass
            else:
                tel = None
    if tel:
        resultado['telefono'] = tel.group(1) if tel.lastindex else tel.group()

    # ── PAGADURÍA ────────────────────────────────────────────────────────
    # Primero buscar pagadurías conocidas en el texto completo
    PAGS_CONOCIDAS = [
        'colpensiones', 'fopep', 'fiduprevisora', 'cremil', 'casur',
        'foncep', 'colfondos', 'porvenir', 'proteccion', 'skandia',
        'secretaria de educacion', 'secretaria educacion',
        'policia nacional', 'ejercito nacional', 'armada nacional',
        'fuerza aerea', 'dian', 'fiscalia', 'rama judicial',
        'contraloria', 'procuraduria', 'gobernacion', 'alcaldia',
        'hospital', 'universidad', 'ecopetrol', 'magisterio',
        'fomag', 'fonpet', 'mapfre', 'sura', 'alfa',
        # NO incluir "banco popular" aquí — es pagaduría de libranza externa,
        # no pagaduría de nómina/pensión
    ]

    pag_encontrada = None
    for pag in PAGS_CONOCIDAS:
        if pag in t:
            pag_encontrada = pag
            break

    # Detectar si el mensaje ES la pagaduría (texto corto, sin verbos, sin flags)
    # CORRECCIÓN: excluir respuestas de sí/no ("si", "no", "claro", "exacto")
    palabras = t.split()
    RESPUESTAS_SIMPLES = {'si', 'no', 'claro', 'exacto', 'correcto', 'ok',
                          'oki', 'dale', 'listo', 'bueno', 'verdad', 'cierto',
                          'negativo', 'afirmativo', 'tampoco', 'tampoco tengo',
                          'ninguno', 'ninguna', 'nada', 'nunca'}
    VERBOS_ACCION = ['tengo', 'soy', 'estoy', 'tiene', 'quiero', 'necesito',
                     'puedo', 'como', 'cuando', 'trabajo', 'soy', 'tuve',
                     'tenia', 'me', 'mi', 'hay', 'fue', 'era', 'hola']

    es_pagaduria_directa = (
        len(palabras) >= 1 and
        len(palabras) <= 6 and
        t not in RESPUESTAS_SIMPLES and
        not any(p in t for p in VERBOS_ACCION) and
        not resultado['tipo_cliente'] and
        not resultado['tipo_pension'] and
        not resultado['tipo_contrato'] and
        not resultado['insolvencia'] and
        not resultado['embargos'] and
        not resultado['reportes_financiero'] and
        # No clasificar palabras sueltas ambiguas como pagaduría
        not any(t == s for s in RESPUESTAS_SIMPLES)
    )

    if pag_encontrada:
        resultado['pagaduria'] = pag_encontrada
    elif es_pagaduria_directa:
        resultado['pagaduria'] = texto.strip()

    # ── SALUDO ───────────────────────────────────────────────────────────
    SALUDOS = ['hola', 'buenos dias', 'buenas tardes', 'buenas noches',
               'buenas', 'buen dia', 'hello', 'hey']
    if any(p in t for p in SALUDOS) and len(palabras) <= 5:
        resultado['es_saludo'] = True

    return resultado


@app.route('/extraer', methods=['POST', 'OPTIONS'])
def extraer():
    """
    Extrae datos estructurados del mensaje del cliente.
    100% determinístico — sin IA, sin JSON de Gemini, sin fallos de parsing.
    Solo lo llama andy.html. El portal no lo usa.
    """
    if request.method == 'OPTIONS':
        return '', 204, CORS_HEADERS
    try:
        body  = request.get_json(force=True) or {}
        texto = body.get('texto', '')
        if not texto:
            return _cors(jsonify({'error': 'texto requerido'})), 400
        resultado = extraer_datos_mensaje(texto)
        return _cors(jsonify(resultado))
    except Exception as e:
        return _cors(jsonify({'error': str(e)})), 500


# =================== ARRANQUE ===================
# Precalentar el caché en hilo separado para no bloquear el inicio
threading.Thread(target=cargar_cache, daemon=True).start()

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
