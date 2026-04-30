from flask import Flask, request, jsonify, send_file
from fillpdf import fillpdfs
import requests
import io
import os
import tempfile

app = Flask(__name__)

PDF_URL = "https://portal.grupofipcol.com/Formatos/Area_comercial/Banco_Union/AUTORIZACIONES_DE_CONSULTA/AUTORIZACION_CONSULTAS_OCT_2025.pdf"


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
    monto_letras = num_letras(monto_num) + ' PESOS' if monto_num > 0 else ''

    pdf_base_path = os.path.join(os.path.dirname(__file__), 'FT449_base.pdf')

    campos = {
        'fecha_dia': dd,
        'fecha_mes': mm,
        'fecha_anio': aaaa,
        'valor_solicitado': monto_str + ' - ' + monto_letras,
        'plazo': plazo,
        'pagaduria': pagaduria,
        'destino_del_credito': destino,
        'apellidos': apellidos,
        'nombres': nombres,
        'tipo_identificacion': 'X',
        'numero_identificacion': cedula,
        'direccion_residencia': direccion,
        'ciudad_residencia': ciudad,
        'numero_celular': telefono,
        'tipo_originador': 'X',
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


CORS_HEADERS = {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'POST, GET, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type'
}


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
