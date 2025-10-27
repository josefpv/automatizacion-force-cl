import os
import time
from datetime import date, datetime, timedelta
import sqlite3
import requests
import logging
from dotenv import load_dotenv
import subprocess
import tempfile
from pdf2image import convert_from_bytes
import numpy as np

#justes de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler("bsale_sync.log"),
        logging.StreamHandler()
    ]
)

#Se cargan variables de entorno
load_dotenv()

BSALE_TOKEN = os.getenv("BSALE_TOKEN")
BSALE_BASE_URL = os.getenv("BSALE_BASE_URL")
INTERVALO_MINUTOS = 5
HEADERS = {
    "access_token": BSALE_TOKEN,
    "Content-Type": "application/json"
}
PAPER_WIDTH_MM = int(os.getenv("PAPER_WIDTH_MM", "58"))   # ancho 
PAPER_HEIGHT_MM = int(os.getenv("PAPER_HEIGHT_MM", "297")) #alto

#timestamp
current_date_obj = date.today() - timedelta(days=1)
current_datetime = datetime.combine(current_date_obj, datetime.min.time())
timestamp = int(current_datetime.timestamp())

#conexion sqllite
conn = sqlite3.connect('db/sqlite.db', check_same_thread=False)
cursor = conn.cursor()

#creacion de tabla
cursor.execute("""
CREATE TABLE IF NOT EXISTS documentos_procesados (
    id INTEGER PRIMARY KEY,
    url_pdf TEXT,
    fecha_procesado TEXT
)
""")
conn.commit()

def obtener_documentos():
    #Obtencion documentos desde la API de Bsale
    url = BSALE_BASE_URL + f"documents.json?emissiondate={timestamp}"
    nuevos = []

    while url:
        logging.info(f"Consultando: {url}")
        r = requests.get(url, headers=HEADERS)
        data = r.json()

        for item in data.get("items", []):
            doc_id = item["id"]
            url_pdf = item.get("urlPdfOriginal")

            if not documento_ya_procesado(doc_id):
                nuevos.append({
                    "id": doc_id,
                    "pdf": url_pdf
                })
            break

        url = None # data.get("next")  #  verifica si quedan paginas por consultar

    return nuevos

def documento_ya_procesado(doc_id):
    #Revisa si un documento ya fue procesado
    cursor.execute("SELECT 1 FROM documentos_procesados WHERE id = ?", (doc_id,))
    return cursor.fetchone() is not None

def procesar_documentos(documentos):
    #Procesa los documentos nuevos.
    for d in documentos:
        logging.info(f"Procesando documento {d['id']} - PDF: {d['pdf']}")

        # impresión de boleta:
        try:
            imprimir_pdf_como_imagen(d["pdf"], impresora="XP-58IIH")
            guardar_documento(d["id"], d["pdf"])
        except Exception as e:
            logging.error(f"Error al imprimir documento {d['id']}: {e}")

        guardar_documento(d["id"], d["pdf"])

def es_pagina_blanca(img, umbral=0.99):
    arr = np.array(img)
    # Verifica cuántos píxeles son blancos
    blancos = np.all(arr == 255, axis=-1)
    porcentaje_blancos = np.mean(blancos)
    return porcentaje_blancos >= umbral

def enviar_comando_corte_win(printer_name):
    try:
        import win32print
    except Exception:
        logging.warning("pywin32 no disponible: no se puede enviar comando de corte")
        return

    # Secuencias comúnmente soportadas por impresoras térmicas (GS V, etc.)
    sequences = [
        b'\x1dV\x00',       # GS V 0  (full/partial depende del modelo)
        b'\x1dV\x01',       # GS V 1
        b'\x1dV\x41\x00',   # GS V A n  (algunos modelos)
    ]

    hPrinter = None
    try:
        hPrinter = win32print.OpenPrinter(printer_name)
        # Doc info: enviar en modo RAW
        doc_info = ("Python ESC/POS Cut", None, "RAW")
        win32print.StartDocPrinter(hPrinter, 1, doc_info)
        win32print.StartPagePrinter(hPrinter)

        for seq in sequences:
            try:
                win32print.WritePrinter(hPrinter, seq)
            except Exception:
                # ignorar secuencia fallida y probar la siguiente
                continue

        win32print.EndPagePrinter(hPrinter)
        win32print.EndDocPrinter(hPrinter)
    except Exception as e:
        logging.warning(f"No se pudo enviar comando de corte a {printer_name}: {e}")
    finally:
        if hPrinter:
            win32print.ClosePrinter(hPrinter)

def imprimir_pdf_como_imagen(url_pdf, impresora):
    # Descargar PDF
    pdf_data = requests.get(url_pdf).content

    # Ruta para Windows: usar pywin32 + PIL para enviar imágenes a la impresora GDI
    if os.name == 'nt':
        try:
            import win32print
            import win32ui
            import win32con
            from PIL import Image, ImageWin
        except Exception as e:
            raise RuntimeError("Para imprimir en Windows instale pywin32 y Pillow: pip install pywin32 Pillow") from e

        # Convertir PDF a imágenes (ajustar dpi si se necesita mayor/n menor detalle)
        images = convert_from_bytes(pdf_data, dpi=203)  # 203 dpi es común en impresoras térmicas

        printer_name = impresora or win32print.GetDefaultPrinter()
        logging.info(f"Imprimiendo en Windows en impresora: {printer_name}")

        for page_num, img in enumerate(images, start=1):
            # Asegurar modo RGB
            if img.mode != "RGB":
                img = img.convert("RGB")

            if es_pagina_blanca(img):
                logging.info(f"Página {page_num} omitida por estar en blanco")
                continue
            
            # Crear DC para la impresora
            hDC = win32ui.CreateDC()
            hDC.CreatePrinterDC(printer_name)
            try:
                # DPI del dispositivo (log pixels)
                dpi_x = hDC.GetDeviceCaps(win32con.LOGPIXELSX)
                dpi_y = hDC.GetDeviceCaps(win32con.LOGPIXELSY)

                # Convertir mm a píxeles (1 inch = 25.4 mm)
                target_printable_width_px = int(PAPER_WIDTH_MM * dpi_x / 25.4)
                target_printable_height_px = int(PAPER_HEIGHT_MM * dpi_y / 25.4)

                # Si el driver reporta area imprimible, tomar el mínimo entre el declarado y el calculado
                printable_width = hDC.GetDeviceCaps(win32con.HORZRES)
                printable_height = hDC.GetDeviceCaps(win32con.VERTRES)

                usable_width = min(printable_width, target_printable_width_px)
                usable_height = min(printable_height, target_printable_height_px)

                # Escalar imagen para caber en el ancho imprimible (preferible en térmicas)
                img_width, img_height = img.size
                ratio = min(usable_width / img_width, usable_height / img_height, 1.0)  # no agrandar
                target_width = int(img_width * ratio)
                target_height = int(img_height * ratio)
                img_resized = img.resize((target_width, target_height), resample=Image.LANCZOS)

                dib = ImageWin.Dib(img_resized)

                hDC.StartDoc(f"bsale_print_page_{page_num}")
                hDC.StartPage()

                # Alinear la imagen al tope (y=0) para evitar huecos superiores en impresoras térmicas
                # Centrar horizontalmente respecto a la anchura física calculada
                phys_width = hDC.GetDeviceCaps(win32con.PHYSICALWIDTH)
                x = int((phys_width - target_width) / 2) if phys_width > target_width else 0
                y = 0  # imprime desde arriba

                # Dibujar la imagen en el DC de la impresora
                dib.draw(hDC.GetHandleOutput(), (x, y, x + target_width, y + target_height))

                hDC.EndPage()
                hDC.EndDoc()
                logging.info(f"Página {page_num} enviada a la impresora {printer_name}")
            finally:
                hDC.DeleteDC()

        try:
            enviar_comando_corte_win(printer_name)
        except Exception as e:
            logging.warning(f"Error al solicitar corte: {e}")

        logging.info(f"Documento impreso en {printer_name}")
        return

    # Comportamiento original para sistemas tipo Unix: guardar temporal y usar lp
    pdf_path = tempfile.mktemp(suffix=".pdf")
    with open(pdf_path, 'wb') as f:
        f.write(pdf_data)

    subprocess.run([
        "lp",
        "-d", impresora,
        "-o", "media=Custom.58x297mm",
        "-o", "fit-to-page",
        pdf_path
    ], check=True)

    # Limpiar archivo temporal
    os.unlink(pdf_path)
    logging.info(f"Documento impreso en {impresora}")

def guardar_documento(doc_id, url_pdf):
    #Guarda el documento como procesado
    fecha = datetime.now().isoformat()
    cursor.execute(
        "INSERT OR IGNORE INTO documentos_procesados (id, url_pdf, fecha_procesado) VALUES (?, ?, ?)",
        (doc_id, url_pdf, fecha)
    )
    conn.commit()

def consulta_cantidad_procesados():
    #Consulta la cantidad de documentos procesados
    cursor.execute("SELECT COUNT(*) FROM documentos_procesados")
    count = cursor.fetchone()[0]
    logging.info(f"Total de documentos procesados: {count}")

def eliminar_registros():
    #Elimina todos los registros de documentos procesados
    cursor.execute("DELETE FROM documentos_procesados")
    conn.commit()
    logging.info("Registros eliminados.")

def consulta_documento(doc_id):
    #Consulta un documento por su ID
    cursor.execute("SELECT * FROM documentos_procesados")
    registro = cursor.fetchone()
    if registro:
        logging.info(f"Documento encontrado: ID={registro[0]}, URL_PDF={registro[1]}, Fecha_Procesado={registro[2]}")
    else:
        logging.info("Documento no encontrado.")

def main():

    
    eliminar_registros()  # Descomentar para eliminar registros al iniciar

    while True:
        try:
            nuevos = obtener_documentos()
            if nuevos:
                logging.info(f"Se encontraron {len(nuevos)} documentos nuevos.")
                procesar_documentos(nuevos)
            else:
                logging.info("No hay documentos nuevos.")

        except Exception as e:
            logging.error(f"Error: {e}")

        logging.info(f"Esperando {INTERVALO_MINUTOS} minutos...\n")
        time.sleep(INTERVALO_MINUTOS * 60)


if __name__ == "__main__":
    main()

