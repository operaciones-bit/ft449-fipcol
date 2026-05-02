from flask import Flask, request, jsonify, send_file
from fillpdf import fillpdfs
import requests
import io
import os
import tempfile
import unicodedata
import re

app = Flask(__name__)

# =================== CONFIGURACION ===================
PDF_URL = "https://portal.grupofipcol.com/Formatos/Area_comercial/Banco_Union/AUTORIZACIONES_DE_CONSULTA/AUTORIZACION_CONSULTAS_OCT_2025.pdf"
APPS_SCRIPT_URL = "https://script.google.com/macros/s/AKfycby9sxBx8XVMAxcs3MWtB_LmcQjqP5e2Bn_UIlCurpvs40LSkgyY3A6AuuNF_D_Ti24KIQ/exec"
SMLV = 1423500

CORS_HEADERS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type'
}

# =================== UTILIDADES ===================
def normalizar_texto(texto):
    """Quita tildes y convierte a minúsculas para comparaciones flexibles"""
    if not texto:
        return ''
    texto = texto.lower().strip()
    texto = unicodedata.normalize('NFD', texto)
    texto = ''.join(c for c in texto if unicodedata.category(c) != 'Mn')
    return texto

def similitud(texto1, texto2):
    """Verifica si texto1 está contenido en texto2 o viceversa, de forma flexible"""
    t1 = normalizar_texto(texto1)
    t2 = normalizar_texto(texto2)
    # Buscar palabras clave del texto1 en texto2
    palabras = [p for p in t1.split() if len(p) > 3]
    if not palabras:
        return False
    coincidencias = sum(1 for p in palabras if p in t2)
    return coincidencias >= max(1, len(palabras) * 0.6)

def llamar_apps_script(params, method='GET'):
    """Llama al Apps Script con GET o POST"""
    try:
        if method == 'GET':
            r = requests.get(APPS_SCRIPT_URL, params=params, timeout=30, allow_redirects=True)
        else:
            r = requests.post(APPS_SCRIPT_URL, json=params, timeout=30, allow_redirects=True)
        return r.json()
    except Exception as e:
        return {'error': str(e)}

# =================== FUNCIONES CORE ===================

def verificar_pagaduria(texto_pagaduria, tipo_cliente='activo'):
    """
    Busca la pagaduria en el CRM de forma flexible.
    Retorna las entidades que la atienden o lista vacía si no existe.
    """
    try:
        data = llamar_apps_script({'action': 'getPagadurias'})
        if not data or 'error' in data:
            return {'encontrada': False, 'entidades': [], 'pagaduria_oficial': ''}

        lista = data.get('pensionados' if tipo_cliente == 'pensionado' else 'activos', [])
        
        entidades_encontradas = []
        pagaduria_oficial = ''
        
        for item in lista:
            pag_crm = item.get('pagaduria', '')
            entidad = item.get('entidad', '')
            if similitud(texto_pagaduria, pag_crm):
                if entidad not in entidades_encontradas:
                    entidades_encontradas.append(entidad)
                if not pagaduria_oficial:
                    pagaduria_oficial = pag_crm

        return {
            'encontrada': len(entidades_encontradas) > 0,
            'entidades': entidades_encontradas,
            'pagaduria_oficial': pagaduria_oficial,
            'texto_original': texto_pagaduria
        }
    except Exception as e:
        return {'encontrada': False, 'entidades': [], 'error': str(e)}


def calcular_viabilidad(perfil):
    """
    Consulta las politicas del CRM y determina que entidades aplican.
    perfil = {
        tipo_cliente, pagaduria, tipo_contrato, tipo_pension,
        reportes_financiero, reportes_real, reportes_telco,
        libranza_negativa, cooperativa_negativa, insolvencia,
        procesos_juridicos, embargos, tipo_credito, edad
    }
    """
    try:
        params = {
            'action': 'analizarViabilidad',
            'tipoCliente': perfil.get('tipo_cliente', 'activo'),
            'pagaduria': perfil.get('pagaduria', ''),
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
        resultado = llamar_apps_script(params)
        if not resultado or 'error' in resultado:
            return {'viables': [], 'no_viables': [], 'error': 'No se pudo consultar politicas'}

        viables = []
        no_viables = []
        for r in resultado:
            if r.get('viable'):
                viables.append({
                    'entidad': r.get('entidad'),
                    'condiciones': [i for i in (r.get('issues') or []) if i.startswith('⚠️')]
                })
            else:
                no_viables.append({
                    'entidad': r.get('entidad'),
                    'motivos': [i for i in (r.get('issues') or []) if i.startswith('❌')]
                })

        return {'viables': viables, 'no_viables': no_viables}
    except Exception as e:
        return {'viables': [], 'no_viables': [], 'error': str(e)}


def calcular_capacidad(salario, descuentos_ley, otros_descuentos, segmento=''):
    """Calcula la capacidad de pago segun el segmento"""
    segmentos_especiales = ['secretarias_docente', 'mindefensa_cremil']
    if segmento in segmentos_especiales and salario < SMLV * 2:
        capacidad = salario - descuentos_ley - SMLV - otros_descuentos
    else:
        capacidad = ((salario - descuentos_ley) / 2) - otros_descuentos - 10000
    return max(0, round(capacidad))


def calcular_ofertas(capacidad, entidades_viables, pagaduria='', tipo_cliente='activo'):
    """
    Calcula las ofertas para cada entidad viable.
    Consulta los factores reales del CRM.
    """
    ofertas = []
    plazo = 144
    tasa_bu = 1.60

    for entidad_info in entidades_viables:
        entidad = entidad_info.get('entidad', '') if isinstance(entidad_info, dict) else entidad_info
        entidad_lower = entidad.lower()

        try:
            # BANCO UNION
            if 'banco' in entidad_lower or 'union' in entidad_lower:
                r = llamar_apps_script({'action': 'getFactor', 'tasa': tasa_bu, 'plazo': plazo})
                factor = float(r) if r and not isinstance(r, dict) else None
                if factor and capacidad > 0:
                    monto = int((capacidad / factor) * 1000000 / 100000) * 100000
                    if monto >= 1000000:
                        pag_lower = normalizar_texto(pagaduria)
                        pct_fga = 0.06 if tipo_cliente == 'pensionado' else 0.08
                        if 'policia' in pag_lower or 'fiduprevisora' in pag_lower:
                            pct_fga = 0.10
                        elif 'cremil' in pag_lower or 'casur' in pag_lower:
                            pct_fga = 0.09
                        fga = monto * pct_fga * 1.19
                        # Gestion documental
                        if monto <= 10000000: gestion = 403058
                        elif monto <= 20000000: gestion = 1138638
                        elif monto <= 30000000: gestion = 1897730
                        elif monto <= 40000000: gestion = 2150761
                        elif monto <= 50000000: gestion = 2372163
                        elif monto <= 60000000: gestion = 2625192
                        else: gestion = 2820814
                        neto = monto - fga - gestion
                        ofertas.append({
                            'entidad': 'Banco Unión',
                            'tasa': '1.60%',
                            'plazo': f'{plazo} meses',
                            'monto': monto,
                            'neto': round(neto),
                            'cuota': capacidad,
                            'descuentos': f'FGA: ${round(fga):,} + Gestión: ${gestion:,}'.replace(',', '.')
                        })

            # RAYCO
            elif 'rayco' in entidad_lower:
                plazo_r = min(plazo, 120)
                r = llamar_apps_script({'action': 'getFactorRayco', 'plazo': plazo_r})
                factor = float(r) if r and not isinstance(r, dict) else None
                if factor and capacidad > 0:
                    monto = int((capacidad / factor) * 1000000 / 100000) * 100000
                    if monto >= 1000000:
                        neto = monto * 0.91
                        ofertas.append({
                            'entidad': 'Rayco',
                            'tasa': '1.89%',
                            'plazo': f'{plazo_r} meses',
                            'monto': monto,
                            'neto': round(neto),
                            'cuota': capacidad,
                            'descuentos': f'Descuento: ${round(monto * 0.09):,}'.replace(',', '.')
                        })

            # KALA
            elif 'kala' in entidad_lower:
                r = llamar_apps_script({'action': 'getFactorKala', 'plazo': plazo})
                factor = float(r) if r and not isinstance(r, dict) else None
                if factor and capacidad > 0:
                    monto = int((capacidad / (factor / 100)) / 100000) * 100000
                    if monto >= 1000000:
                        corr = monto * 0.07
                        fian = monto * 0.03
                        seg = monto * 0.00125
                        neto = monto - corr - fian - seg - 22598
                        ofertas.append({
                            'entidad': 'Kala',
                            'tasa': '1.91%',
                            'plazo': f'{plazo} meses',
                            'monto': monto,
                            'neto': round(neto),
                            'cuota': capacidad,
                            'descuentos': 'Corretetaje 7% + Fianza 3% + Seguro'
                        })

            # FINEXUS
            elif 'finexus' in entidad_lower:
                r = llamar_apps_script({'action': 'getFactorFinexus', 'tasa': 1.80, 'plazo': plazo})
                factor = float(r) if r and not isinstance(r, dict) else None
                if factor and capacidad > 0:
                    monto = int((capacidad / factor) * 1000000 / 100000) * 100000
                    if monto >= 1000000:
                        neto = monto * 0.77
                        ofertas.append({
                            'entidad': 'Finexus',
                            'tasa': '1.80%',
                            'plazo': f'{plazo} meses',
                            'monto': monto,
                            'neto': round(neto),
                            'cuota': capacidad,
                            'descuentos': 'Descuento: 23%'
                        })
        except Exception as e:
            continue

    return ofertas


# =================== ENDPOINT PRINCIPAL ANDY ===================
@app.route('/consultar', methods=['POST', 'OPTIONS'])
def consultar():
    """
    Endpoint principal que Andy llama para verificaciones y calculos.
    Recibe: { accion, datos }
    Devuelve: resultado segun la accion
    """
    if request.method == 'OPTIONS':
        return '', 204, CORS_HEADERS

    try:
        body = request.get_json(force=True) or {}
        accion = body.get('accion', '')
        datos = body.get('datos', {})

        # VERIFICAR PAGADURIA
        if accion == 'verificar_pagaduria':
            pagaduria = datos.get('pagaduria', '')
            tipo_cliente = datos.get('tipo_cliente', 'activo')
            resultado = verificar_pagaduria(pagaduria, tipo_cliente)
            response = jsonify(resultado)
            for k, v in CORS_HEADERS.items():
                response.headers[k] = v
            return response

        # CALCULAR VIABILIDAD
        elif accion == 'calcular_viabilidad':
            resultado = calcular_viabilidad(datos)
            response = jsonify(resultado)
            for k, v in CORS_HEADERS.items():
                response.headers[k] = v
            return response

        # CALCULAR CAPACIDAD
        elif accion == 'calcular_capacidad':
            salario = float(datos.get('salario', 0))
            descuentos_ley = float(datos.get('descuentos_ley', 0))
            otros_descuentos = float(datos.get('otros_descuentos', 0))
            segmento = datos.get('segmento', '')
            capacidad = calcular_capacidad(salario, descuentos_ley, otros_descuentos, segmento)
            response = jsonify({'capacidad': capacidad})
            for k, v in CORS_HEADERS.items():
                response.headers[k] = v
            return response

        # CALCULAR OFERTAS
        elif accion == 'calcular_ofertas':
            capacidad = float(datos.get('capacidad', 0))
            entidades_viables = datos.get('entidades_viables', [])
            pagaduria = datos.get('pagaduria', '')
            tipo_cliente = datos.get('tipo_cliente', 'activo')
            ofertas = calcular_ofertas(capacidad, entidades_viables, pagaduria, tipo_cliente)
            response = jsonify({'ofertas': ofertas})
            for k, v in CORS_HEADERS.items():
                response.headers[k] = v
            return response

        # PROCESO COMPLETO: viabilidad + ofertas en una sola llamada
        elif accion == 'analizar_perfil_completo':
            # 1. Verificar pagaduria
            pag_resultado = verificar_pagaduria(
                datos.get('pagaduria', ''),
                datos.get('tipo_cliente', 'activo')
            )
            if not pag_resultado.get('encontrada'):
                response = jsonify({
                    'pagaduria_encontrada': False,
                    'mensaje': f"La pagaduría '{datos.get('pagaduria')}' no aparece en nuestro sistema para ninguna entidad.",
                    'viables': [],
                    'ofertas': []
                })
                for k, v in CORS_HEADERS.items():
                    response.headers[k] = v
                return response

            # 2. Calcular viabilidad con pagaduria oficial
            datos['pagaduria'] = pag_resultado.get('pagaduria_oficial', datos.get('pagaduria'))
            viabilidad = calcular_viabilidad(datos)
            viables = viabilidad.get('viables', [])
            no_viables = viabilidad.get('no_viables', [])

            # 3. Calcular ofertas si hay capacidad
            ofertas = []
            capacidad = float(datos.get('capacidad', 0))
            if capacidad > 0 and viables:
                ofertas = calcular_ofertas(
                    capacidad, viables,
                    datos.get('pagaduria', ''),
                    datos.get('tipo_cliente', 'activo')
                )

            response = jsonify({
                'pagaduria_encontrada': True,
                'pagaduria_oficial': pag_resultado.get('pagaduria_oficial'),
                'viables': viables,
                'no_viables': no_viables,
                'ofertas': ofertas,
                'capacidad': capacidad
            })
            for k, v in CORS_HEADERS.items():
                response.headers[k] = v
            return response

        else:
            response = jsonify({'error': f'Accion desconocida: {accion}'})
            for k, v in CORS_HEADERS.items():
                response.headers[k] = v
            return response, 400

    except Exception as e:
        response = jsonify({'error': str(e)})
        for k, v in CORS_HEADERS.items():
            response.headers[k] = v
        return response, 500


# =================== ENDPOINT FT-449 (SIN CAMBIOS) ===================
def num_letras(n):
    u = ['','UN','DOS','TRES','CUATRO','CINCO','SEIS','SIETE','OCHO','NUEVE',
         'DIEZ','ONCE','DOCE','TRECE','CATORCE','QUINCE','DIECISEIS',
         'DIECISIETE','DIECIOCHO','DIECINUEVE']
    d = ['','','VEINTE','TREINTA','CUARENTA','CINCUENTA',
         'SESENTA','SETENTA','OCHENTA','NOVENTA']
    c = ['','CIENTO','DOSCIENTOS','TRESCIENTOS','CUATROCIENTOS','QUINIENTOS',
         'SEISCIENTOS','SETECIENTOS','OCHOCIENTOS','NOVECIENTOS']
    def bloque(num):
        if num == 0: return ''
        if num == 100: return 'CIEN'
        r = ''
        if num >= 100:
            r = c[num // 100] + ' '
            num %= 100
        if num >= 20:
            r += d[num // 10]
            if num % 10: r += ' Y ' + u[num % 10]
        elif num > 0:
            r += u[num]
        return r.strip()
    r = ''
    m = round(n)
    if m >= 1000000:
        b = m // 1000000
        r += ('UN MILLON' if b == 1 else bloque(b) + ' MILLONES') + ' '
        m %= 1000000
    if m >= 1000:
        b = m // 1000
        r += ('MIL' if b == 1 else bloque(b) + ' MIL') + ' '
        m %= 1000
    if m > 0:
        r += bloque(m)
    return r.strip()

def separar_nombre(nombre_completo):
    partes = nombre_completo.strip().upper().split()
    if len(partes) >= 4:
        return ' '.join(partes[:2]), ' '.join(partes[2:])
    elif len(partes) == 3:
        return partes[0], ' '.join(partes[1:])
    elif len(partes) == 2:
        return partes[0], partes[1]
    return nombre_completo.upper(), ''

def generar_pdf_ft449(datos):
    apellidos, nombres = separar_nombre(datos.get('nombre', ''))
    cedula    = datos.get('cedula', '')
    telefono  = datos.get('telefono', '')
    pagaduria = datos.get('pagaduria', '').upper()
    plazo     = str(datos.get('plazo', '144')) + ' meses'
    direccion = datos.get('direccion', '').upper()
    ciudad    = datos.get('ciudad', '').upper()
    destino   = datos.get('destino', 'Libre Inversion').upper()
    fecha_hoy = datos.get('fechaHoy', '')
    partes_fecha = fecha_hoy.split('/')
    dd   = partes_fecha[0] if len(partes_fecha) > 0 else ''
    mm   = partes_fecha[1] if len(partes_fecha) > 1 else ''
    aaaa = partes_fecha[2] if len(partes_fecha) > 2 else ''
    monto_num = datos.get('montoNum', 0)
    try:
        monto_num = int(str(monto_num).replace('.','').replace(',','').replace('$',''))
    except:
        monto_num = 0
    monto_str = '$' + f'{monto_num:,}'.replace(',', '.')
    pdf_base_path = os.path.join(os.path.dirname(__file__), 'FT449_base.pdf')
    campos = {
        'fecha_dia': dd,
        'fecha_mes': mm,
        'fecha_anio': aaaa,
        'valor_solicitado': monto_str,
        'plazo': plazo,
        'pagaduria': pagaduria,
        'destino_del_credito': destino,
        'apellidos': apellidos,
        'nombres': nombres,
        'tipo_identificacion': 'CC',
        'numero_identificacion': cedula,
        'direccion_residencia': direccion,
        'ciudad_residencia': ciudad,
        'numero_celular': telefono,
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
    except:
        pass
    return output

@app.route('/', methods=['POST', 'GET', 'OPTIONS'])
def endpoint_ft449():
    if request.method == 'OPTIONS':
        return '', 204, CORS_HEADERS
    if request.method == 'GET':
        datos = {
            'nombre': 'JESUS EDILSON QUINTERO VALLEJO',
            'cedula': '9776983',
            'telefono': '3168671944',
            'pagaduria': 'FIDUPREVISORA',
            'montoNum': 123700000,
            'plazo': '144',
            'direccion': 'CALLE 6N # 4-200',
            'ciudad': 'PIEDECUESTA',
            'fechaHoy': '30/04/2026',
            'destino': 'Libre Inversion'
        }
    else:
        datos = request.get_json(force=True) or {}
    try:
        pdf_bytes = generar_pdf_ft449(datos)
        nombre_archivo = f"FT449_{datos.get('cedula','cliente')}.pdf"
        response = send_file(
            pdf_bytes,
            mimetype='application/pdf',
            as_attachment=True,
            download_name=nombre_archivo
        )
        for k, v in CORS_HEADERS.items():
            response.headers[k] = v
        return response
    except Exception as e:
        return jsonify({'error': str(e)}), 500, CORS_HEADERS

@app.route('/ping', methods=['GET'])
def ping():
    return jsonify({'status': 'ok', 'mensaje': 'Servidor Python Fipcol activo'}), 200

if __name__ == '__main__':
    app.run(debug=False)
