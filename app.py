"""
Sistema de QR dinámico para mantenimiento preventivo de equipo médico
----------------------------------------------------------------------
- El QR de cada equipo apunta SIEMPRE a la misma URL: /equipo/<id>
- Esa página lee los datos más recientes desde la base de datos SQLite,
  así que al actualizar un mantenimiento NO hay que reimprimir el QR.
- Si pasa más de 1 año (365 días) desde el último mantenimiento sin
  registrar uno nuevo, el equipo se marca automáticamente como
  inactivo y desaparece del listado de clientes activos.

Cómo correrlo:
    pip install -r requirements.txt
    python app.py
Luego abre http://127.0.0.1:5000/admin

IMPORTANTE: cambia BASE_URL por el dominio real donde se publique la
app antes de generar los QR definitivos (los QR ya generados con una
URL vieja dejarían de funcionar si cambias el dominio después).
"""

import os
import sqlite3
from datetime import datetime, date
from flask import (
    Flask, render_template, request, redirect, url_for,
    send_from_directory, flash, abort
)
from werkzeug.utils import secure_filename
import qrcode

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "mantenimiento.db")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
QR_FOLDER = os.path.join(BASE_DIR, "static", "qrcodes")
LOGO_PATH = "/static/logo.png"  # coloca tu logo en static/logo.png

# Cambia esto por tu dominio real al desplegar (ej. "https://simedic.mx")
BASE_URL = "http://192.168.0.139:5000"

DIAS_LIMITE_INACTIVO = 365  # 1 año sin renovar -> se elimina del listado

app = Flask(__name__)
app.secret_key = "cambia-esta-clave-en-produccion"
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB máx por archivo

ALLOWED_EXT = {"pdf"}


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


# ---------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS equipos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero_cliente TEXT NOT NULL UNIQUE,
            nombre_equipo TEXT NOT NULL,
            ubicacion TEXT,
            fecha_ultimo_mant TEXT NOT NULL,
            fecha_proximo_mant TEXT NOT NULL,
            tecnico TEXT NOT NULL,
            hoja_servicio_archivo TEXT,
            activo INTEGER NOT NULL DEFAULT 1,
            fecha_registro TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def marcar_inactivos_vencidos():
    """Si pasó más de 1 año desde el último mantenimiento sin renovar,
    el equipo se marca inactivo (se 'elimina' del listado de clientes)."""
    conn = get_db()
    equipos = conn.execute(
        "SELECT id, fecha_ultimo_mant FROM equipos WHERE activo = 1"
    ).fetchall()
    hoy = date.today()
    for eq in equipos:
        f_ultimo = datetime.strptime(eq["fecha_ultimo_mant"], "%Y-%m-%d").date()
        if (hoy - f_ultimo).days > DIAS_LIMITE_INACTIVO:
            conn.execute("UPDATE equipos SET activo = 0 WHERE id = ?", (eq["id"],))
    conn.commit()
    conn.close()


def generar_qr(equipo_id):
    """Genera (o sobrescribe) el PNG del QR para un equipo. La URL
    codificada nunca cambia mientras el id no cambie, así el QR físico
    pegado en el equipo sigue siendo válido para siempre."""
    url_publica = f"{BASE_URL}{url_for('ver_equipo', equipo_id=equipo_id)}"
    img = qrcode.make(url_publica)
    ruta = os.path.join(QR_FOLDER, f"equipo_{equipo_id}.png")
    img.save(ruta)
    return ruta


# ---------------------------------------------------------------------
# Rutas ADMIN (uso interno de SIMEDIC)
# ---------------------------------------------------------------------
@app.route("/admin")
def admin_lista():
    marcar_inactivos_vencidos()
    conn = get_db()
    activos = conn.execute(
        "SELECT * FROM equipos WHERE activo = 1 ORDER BY fecha_proximo_mant ASC"
    ).fetchall()
    conn.close()
    return render_template("admin_lista.html", equipos=activos)


@app.route("/admin/nuevo", methods=["GET", "POST"])
def admin_nuevo():
    if request.method == "POST":
        numero_cliente = request.form["numero_cliente"].strip()
        nombre_equipo = request.form["nombre_equipo"].strip()
        ubicacion = request.form.get("ubicacion", "").strip()
        fecha_ultimo = request.form["fecha_ultimo_mant"]
        fecha_proximo = request.form["fecha_proximo_mant"]
        tecnico = request.form["tecnico"].strip()

        archivo = request.files.get("hoja_servicio")
        nombre_archivo = None
        if archivo and archivo.filename and allowed_file(archivo.filename):
            nombre_archivo = secure_filename(f"{numero_cliente}_{archivo.filename}")
            archivo.save(os.path.join(UPLOAD_FOLDER, nombre_archivo))

        conn = get_db()
        try:
            cur = conn.execute(
                """INSERT INTO equipos
                   (numero_cliente, nombre_equipo, ubicacion, fecha_ultimo_mant,
                    fecha_proximo_mant, tecnico, hoja_servicio_archivo, activo, fecha_registro)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (numero_cliente, nombre_equipo, ubicacion, fecha_ultimo,
                 fecha_proximo, tecnico, nombre_archivo, date.today().isoformat())
            )
            conn.commit()
            nuevo_id = cur.lastrowid
        except sqlite3.IntegrityError:
            conn.close()
            flash("Ese número de cliente ya existe.", "error")
            return redirect(url_for("admin_nuevo"))
        conn.close()

        generar_qr(nuevo_id)
        flash("Equipo registrado y QR generado correctamente.", "ok")
        return redirect(url_for("admin_lista"))

    return render_template("admin_nuevo.html")


@app.route("/admin/editar/<int:equipo_id>", methods=["GET", "POST"])
def admin_editar(equipo_id):
    conn = get_db()
    equipo = conn.execute("SELECT * FROM equipos WHERE id = ?", (equipo_id,)).fetchone()
    if equipo is None:
        conn.close()
        abort(404)

    if request.method == "POST":
        fecha_ultimo = request.form["fecha_ultimo_mant"]
        fecha_proximo = request.form["fecha_proximo_mant"]
        tecnico = request.form["tecnico"].strip()

        archivo = request.files.get("hoja_servicio")
        nombre_archivo = equipo["hoja_servicio_archivo"]
        if archivo and archivo.filename and allowed_file(archivo.filename):
            nombre_archivo = secure_filename(f"{equipo['numero_cliente']}_{archivo.filename}")
            archivo.save(os.path.join(UPLOAD_FOLDER, nombre_archivo))

        # Sobrescribe el mismo registro: el QR físico no cambia.
        conn.execute(
            """UPDATE equipos SET fecha_ultimo_mant = ?, fecha_proximo_mant = ?,
               tecnico = ?, hoja_servicio_archivo = ?, activo = 1 WHERE id = ?""",
            (fecha_ultimo, fecha_proximo, tecnico, nombre_archivo, equipo_id)
        )
        conn.commit()
        conn.close()
        flash("Mantenimiento actualizado. El QR pegado en el equipo sigue funcionando igual.", "ok")
        return redirect(url_for("admin_lista"))

    conn.close()
    return render_template("admin_editar.html", equipo=equipo)


@app.route("/admin/eliminar/<int:equipo_id>", methods=["POST"])
def admin_eliminar(equipo_id):
    conn = get_db()
    conn.execute("UPDATE equipos SET activo = 0 WHERE id = ?", (equipo_id,))
    conn.commit()
    conn.close()
    flash("Equipo eliminado del listado de clientes.", "ok")
    return redirect(url_for("admin_lista"))


@app.route("/admin/qr/<int:equipo_id>")
def descargar_qr(equipo_id):
    ruta = os.path.join(QR_FOLDER, f"equipo_{equipo_id}.png")
    if not os.path.exists(ruta):
        generar_qr(equipo_id)
    return send_from_directory(QR_FOLDER, f"equipo_{equipo_id}.png", as_attachment=True)


# ---------------------------------------------------------------------
# Ruta PÚBLICA: a donde manda el QR pegado en el equipo
# ---------------------------------------------------------------------
@app.route("/equipo/<int:equipo_id>")
def ver_equipo(equipo_id):
    conn = get_db()
    equipo = conn.execute(
        "SELECT * FROM equipos WHERE id = ? AND activo = 1", (equipo_id,)
    ).fetchone()
    conn.close()
    if equipo is None:
        abort(404)

    # Calcula qué tan cerca está el próximo mantenimiento para mostrar
    # un indicador de estado (vigente / próximo / vencido).
    f_proximo = datetime.strptime(equipo["fecha_proximo_mant"], "%Y-%m-%d").date()
    dias_restantes = (f_proximo - date.today()).days
    if dias_restantes < 0:
        estado = {"clase": "vencido", "texto": f"Vencido hace {abs(dias_restantes)} días"}
    elif dias_restantes <= 30:
        estado = {"clase": "proximo", "texto": f"Vence en {dias_restantes} días"}
    else:
        estado = {"clase": "vigente", "texto": "Vigente"}

    return render_template(
        "equipo_publico.html", equipo=equipo, logo=LOGO_PATH, estado=estado
    )


@app.route("/static/uploads/<path:filename>")
def archivo_subido(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


@app.errorhandler(404)
def no_encontrado(e):
    return render_template("404.html"), 404


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", debug=True)
