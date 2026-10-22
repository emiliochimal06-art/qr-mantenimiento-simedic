"""
Sistema de QR/NFC dinámico para mantenimiento preventivo de equipo médico
----------------------------------------------------------------------
- El QR (o la etiqueta NFC) de cada equipo apunta SIEMPRE a la misma URL:
  /equipo/<id>. Esa página lee los datos más recientes de la base de
  datos, así que al actualizar un mantenimiento NO hay que reimprimir
  ni reescribir nada.
- Si pasa más de 1 año (365 días) desde el último mantenimiento sin
  registrar uno nuevo, el equipo se marca inactivo y desaparece del
  listado de clientes activos.
- Un cliente puede tener varios equipos (el número de cliente NO es
  único); lo que identifica a un equipo es su número de serie, que sí
  debe ser único si se captura.
- El panel /admin está protegido con usuario y contraseña. La ficha
  pública (/equipo/<id>) NO requiere login.

Cómo correrlo:
    pip install -r requirements.txt
    python app.py
Luego abre http://127.0.0.1:5000/admin
"""

import os
import sqlite3
from datetime import datetime, date
from functools import wraps
from flask import (
    Flask, render_template, request, redirect, url_for,
    send_from_directory, flash, abort, session
)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
import qrcode

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "mantenimiento.db")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "static", "uploads")
QR_FOLDER = os.path.join(BASE_DIR, "static", "qrcodes")
LOGO_PATH = "/static/logo.png"

DIAS_LIMITE_INACTIVO = 365

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cambia-esta-clave-en-produccion")
app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

ALLOWED_EXT_PDF = {"pdf"}
ALLOWED_EXT_FOTO = {"png", "jpg", "jpeg"}

# Usuario y contraseña del panel admin. Por defecto: admin / simedic2026.
# CÁMBIALOS configurando ADMIN_USER y ADMIN_PASSWORD como variables de
# entorno en Render (pestaña "Environment").
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD_HASH = generate_password_hash(
    os.environ.get("ADMIN_PASSWORD", "simedic2026")
)


def allowed_file(filename, extensiones):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in extensiones


def login_required(vista):
    @wraps(vista)
    def envoltura(*args, **kwargs):
        if not session.get("autenticado"):
            return redirect(url_for("login", siguiente=request.path))
        return vista(*args, **kwargs)
    return envoltura


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS equipos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            numero_cliente TEXT NOT NULL,
            numero_serie TEXT UNIQUE,
            nombre_equipo TEXT NOT NULL,
            ubicacion TEXT,
            fecha_ultimo_mant TEXT NOT NULL,
            fecha_proximo_mant TEXT NOT NULL,
            tecnico TEXT NOT NULL,
            hoja_servicio_archivo TEXT,
            foto_equipo TEXT,
            activo INTEGER NOT NULL DEFAULT 1,
            fecha_registro TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def marcar_inactivos_vencidos():
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
    url_publica = url_for("ver_equipo", equipo_id=equipo_id, _external=True)
    img = qrcode.make(url_publica)
    ruta = os.path.join(QR_FOLDER, f"equipo_{equipo_id}.png")
    img.save(ruta)
    return ruta


@app.route("/admin/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        usuario = request.form.get("usuario", "").strip()
        contrasena = request.form.get("contrasena", "")
        if usuario == ADMIN_USER and check_password_hash(ADMIN_PASSWORD_HASH, contrasena):
            session["autenticado"] = True
            session.permanent = True
            siguiente = request.args.get("siguiente") or url_for("admin_lista")
            return redirect(siguiente)
        flash("Usuario o contraseña incorrectos.", "error")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def logout():
    session.pop("autenticado", None)
    return redirect(url_for("login"))


@app.route("/admin")
@login_required
def admin_lista():
    marcar_inactivos_vencidos()
    conn = get_db()
    activos = conn.execute(
        "SELECT * FROM equipos WHERE activo = 1 ORDER BY fecha_proximo_mant ASC"
    ).fetchall()
    conn.close()
    return render_template("admin_lista.html", equipos=activos)


@app.route("/admin/nuevo", methods=["GET", "POST"])
@login_required
def admin_nuevo():
    if request.method == "POST":
        numero_cliente = request.form["numero_cliente"].strip()
        numero_serie = request.form.get("numero_serie", "").strip() or None
        nombre_equipo = request.form["nombre_equipo"].strip()
        ubicacion = request.form.get("ubicacion", "").strip()
        fecha_ultimo = request.form["fecha_ultimo_mant"]
        fecha_proximo = request.form["fecha_proximo_mant"]
        tecnico = request.form["tecnico"].strip()

        archivo = request.files.get("hoja_servicio")
        nombre_archivo = None
        if archivo and archivo.filename and allowed_file(archivo.filename, ALLOWED_EXT_PDF):
            nombre_archivo = secure_filename(f"{numero_cliente}_{archivo.filename}")
            archivo.save(os.path.join(UPLOAD_FOLDER, nombre_archivo))

        foto = request.files.get("foto_equipo")
        nombre_foto = None
        if foto and foto.filename and allowed_file(foto.filename, ALLOWED_EXT_FOTO):
            nombre_foto = secure_filename(f"foto_{numero_cliente}_{foto.filename}")
            foto.save(os.path.join(UPLOAD_FOLDER, nombre_foto))

        conn = get_db()
        try:
            cur = conn.execute(
                """INSERT INTO equipos
                   (numero_cliente, numero_serie, nombre_equipo, ubicacion,
                    fecha_ultimo_mant, fecha_proximo_mant, tecnico,
                    hoja_servicio_archivo, foto_equipo, activo, fecha_registro)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (numero_cliente, numero_serie, nombre_equipo, ubicacion, fecha_ultimo,
                 fecha_proximo, tecnico, nombre_archivo, nombre_foto, date.today().isoformat())
            )
            conn.commit()
            nuevo_id = cur.lastrowid
        except sqlite3.IntegrityError:
            conn.close()
            flash("Ese número de serie ya está registrado en otro equipo.", "error")
            return redirect(url_for("admin_nuevo"))
        conn.close()

        generar_qr(nuevo_id)
        flash("Equipo registrado y QR generado correctamente.", "ok")
        return redirect(url_for("admin_lista"))

    return render_template("admin_nuevo.html")


@app.route("/admin/editar/<int:equipo_id>", methods=["GET", "POST"])
@login_required
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
        if archivo and archivo.filename and allowed_file(archivo.filename, ALLOWED_EXT_PDF):
            nombre_archivo = secure_filename(f"{equipo['numero_cliente']}_{archivo.filename}")
            archivo.save(os.path.join(UPLOAD_FOLDER, nombre_archivo))

        foto = request.files.get("foto_equipo")
        nombre_foto = equipo["foto_equipo"]
        if foto and foto.filename and allowed_file(foto.filename, ALLOWED_EXT_FOTO):
            nombre_foto = secure_filename(f"foto_{equipo['numero_cliente']}_{foto.filename}")
            foto.save(os.path.join(UPLOAD_FOLDER, nombre_foto))

        conn.execute(
            """UPDATE equipos SET fecha_ultimo_mant = ?, fecha_proximo_mant = ?,
               tecnico = ?, hoja_servicio_archivo = ?, foto_equipo = ?, activo = 1
               WHERE id = ?""",
            (fecha_ultimo, fecha_proximo, tecnico, nombre_archivo, nombre_foto, equipo_id)
        )
        conn.commit()
        conn.close()
        flash("Mantenimiento actualizado. El QR/NFC pegado en el equipo sigue funcionando igual.", "ok")
        return redirect(url_for("admin_lista"))

    conn.close()
    return render_template("admin_editar.html", equipo=equipo)


@app.route("/admin/eliminar/<int:equipo_id>", methods=["POST"])
@login_required
def admin_eliminar(equipo_id):
    conn = get_db()
    conn.execute("UPDATE equipos SET activo = 0 WHERE id = ?", (equipo_id,))
    conn.commit()
    conn.close()
    flash("Equipo eliminado del listado de clientes.", "ok")
    return redirect(url_for("admin_lista"))


@app.route("/admin/qr/<int:equipo_id>")
@login_required
def descargar_qr(equipo_id):
    ruta = os.path.join(QR_FOLDER, f"equipo_{equipo_id}.png")
    if not os.path.exists(ruta):
        generar_qr(equipo_id)
    return send_from_directory(QR_FOLDER, f"equipo_{equipo_id}.png", as_attachment=True)


@app.route("/equipo/<int:equipo_id>")
def ver_equipo(equipo_id):
    conn = get_db()
    equipo = conn.execute(
        "SELECT * FROM equipos WHERE id = ? AND activo = 1", (equipo_id,)
    ).fetchone()
    conn.close()
    if equipo is None:
        abort(404)

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


init_db()

if __name__ == "__main__":
    puerto = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=puerto, debug=True)
