from flask import Flask, request, jsonify, send_file
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
import PyPDF2
import requests
import io
import os

app = Flask(__name__)

W, H = letter
W_IMG, H_IMG = 1241, 1754
SCALE_X = W / W_IMG
SCALE_Y = H / H_IMG

PDF_URL = "https://portal.grupofipcol.com/Formatos/Area_comercial/Banco_Union/AUTORIZACIONES_DE_CONSULTA/AUTORIZACION_CONSULTAS_OCT_2025.pdf"


def pt(px_x, px_y):
    return round(px_x * SCALE_X, 1), round(H - px_y * SCALE_Y, 1)


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
    # 1. Obtener PDF base del banco
    pdf_original = None
    try:
        resp = requests.get(PDF_URL, timeout=10)
        if resp.status_code == 200:
            pdf_original = io.BytesIO(resp.content)
    except:
        pass

    if pdf_original is None:
        ruta_local = os.path.join(os.path.dirname(__file__), 'FT449_base.pdf')
        with open(ruta_local, 'rb') as f:
            pdf_original = io.BytesIO(f.read())

    # 2. Preparar datos
    apellidos, nombres = separar_nombre(datos.get('nombre', ''))
    cedula    = datos.get('cedula', '')
    telefono  = datos.get('telefono', '')
    pagaduria = datos.get('pagaduria', '').upper()
    monto     = datos.get('monto', '')
    plazo     = str(datos.get('plazo', '144'))
    direccion = datos.get('direccion', '').upper()
    ciudad    = datos.get('ciudad', '').upper()
    destino   = datos.get('destino', 'Libre Inversion')

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
    monto_letras = num_letras(monto_num) + ' PESOS' if monto_num > 0 else ''

    # 3. Crear overlay de texto
    packet = io.BytesIO()
    c = canvas.Canvas(packet, pagesize=letter)

    # Fecha
    c.setFont("Helvetica", 8)
    c.drawString(*pt(588, 268), dd)
    c.drawString(*pt(630, 268), mm)
    c.drawString(*pt(668, 268), aaaa)

    # Sección 1 - Datos solicitud
    c.drawString(*pt(72,  574), monto)
    c.drawString(*pt(297, 574), plazo + ' meses')
    c.drawString(*pt(385, 574), pagaduria)
    c.drawString(*pt(648, 574), destino)
    c.setFont("Helvetica", 7)
    c.drawString(*pt(72,  600), monto_letras[:85])

    # Sección 2 - Info personal fila 1
    c.setFont("Helvetica", 8)
    c.drawString(*pt(72,  895), apellidos)
    c.drawString(*pt(305, 895), nombres)
    c.setFont("Helvetica", 9)
    c.drawString(*pt(556, 896), 'X')
    c.setFont("Helvetica", 8)
    c.drawString(*pt(870, 895), cedula)

    # Sección 2 - Info personal fila 2
    c.drawString(*pt(72,  942), direccion)
    c.drawString(*pt(500, 942), ciudad)
    c.drawString(*pt(850, 942), telefono)

    # Sección 2 - Originador fila 3
    c.setFont("Helvetica", 9)
    c.drawString(*pt(243, 978), 'X')
    c.setFont("Helvetica", 7.5)
    c.drawString(*pt(430, 978), 'Carmen Gonzalez')
    c.drawString(*pt(780, 978), 'Grupo Fipcol SAS')

    # Cedula bajo firma
    c.setFont("Helvetica", 8)
    c.drawString(*pt(530, 1492), cedula)

    c.save()
    packet.seek(0)

    # 4. Merge con PDF original
    overlay  = PyPDF2.PdfReader(packet)
    original = PyPDF2.PdfReader(pdf_original)
    writer   = PyPDF2.PdfWriter()
    page = original.pages[0]
    page.merge_page(overlay.pages[0])
    writer.add_page(page)

    output = io.BytesIO()
    writer.write(output)
    output.seek(0)
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
            'monto': '$123.700.000',
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
