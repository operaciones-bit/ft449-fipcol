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
        if pags and 'error' not in pags:
            _cache['pagadurias'] = pags

        # Tipos de contrato
        tipos = _gs_get({'action': 'getTiposContrato'})
        if tipos and isinstance(tipos, list):
            _cache['tipos_contrato'] = tipos

        _cache['ts'] = time.time()
        print(f"[CACHE] CRM cargado. "
              f"Pagadurías activos: {len((_cache.get('pagadurias') or {}).get('activos', []))} | "
              f"Pensionados: {len((_cache.get('pagadurias') or {}).get('pensionados', []))}")


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
    Busca la pagaduría en el caché (sin llamada al CRM).
    Retorna:
      { encontrada, entidades, pagaduria_oficial }
    """
    cargar_cache()
    data = _cache.get('pagadurias') or {}
    clave = 'pensionados' if tipo_cliente == 'pensionado' else 'activos'
    lista = data.get(clave, [])

    entidades, pag_oficial = [], ''
    for item in lista:
        pag_crm = item.get('pagaduria', '')
        entidad = item.get('entidad', '')
        if _similitud(texto_pag, pag_crm):
            if entidad not in entidades:
                entidades.append(entidad)
            if not pag_oficial:
                pag_oficial = pag_crm

    return {
        'encontrada': len(entidades) > 0,
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


# =================== ARRANQUE ===================
# Precalentar el caché en hilo separado para no bloquear el inicio
threading.Thread(target=cargar_cache, daemon=True).start()

if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
