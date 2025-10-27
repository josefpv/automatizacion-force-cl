import os
import time
from datetime import date, datetime
import sqlite3
import requests
import logging
from dotenv import load_dotenv
import subprocess
import tempfile
from pdf2image import convert_from_bytes

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

#timestamp
current_date_obj = date.today()
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
        
def imprimir_pdf_como_imagen(url_pdf, impresora):
    # Descargar PDF
    pdf_data = requests.get(url_pdf).content
    
    # Guardar PDF temporalmente
    pdf_path = tempfile.mktemp(suffix=".pdf")
    with open(pdf_path, 'wb') as f:
        f.write(pdf_data)

    # Imprimir PDF directamente
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

    consulta_documento(11)
    #eliminar_registros()  # Descomentar para eliminar registros al iniciar

    # while True:
    #     try:
    #         nuevos = obtener_documentos()
    #         if nuevos:
    #             logging.info(f"Se encontraron {len(nuevos)} documentos nuevos.")
    #             procesar_documentos(nuevos)
    #         else:
    #             logging.info("No hay documentos nuevos.")

    #     except Exception as e:
    #         logging.error(f"Error: {e}")

    #     logging.info(f"Esperando {INTERVALO_MINUTOS} minutos...\n")
    #     time.sleep(INTERVALO_MINUTOS * 60)


if __name__ == "__main__":
    main()

