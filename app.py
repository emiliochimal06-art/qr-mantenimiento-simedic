"""
Sistema de QR/NFC dinámico para mantenimiento preventivo de equipo médico
----------------------------------------------------------------------
- El QR (o la etiqueta NFC) de cada equipo apunta SIEMPRE a la misma URL:
  /equipo/<id>. Esa página lee los datos más recientes de la base de
  datos, así que al actualizar un mantenimiento NO hay que reimprimir
  ni reescribir nada.
- Base de datos permanente: si existe la variable de entorno
  DATABASE_URL se usa Postgres (Neon); si no, SQLite local para pruebas.
  Las fotos y los PDFs se guardan dentro de la misma base de datos, así
  que no se pierden cuando Render reinicia.
- Un cliente puede tener varios equipos (el número de cliente NO es
  único); lo que identifica a un equipo es su número de serie.
- El panel /admin requiere usuario y contraseña. La ficha pública
  (/equipo/<id>) y sus archivos NO requieren login.

Cómo correrlo en tu compu (usa SQLite local):
    pip install -r requirements.txt
    python app.py
"""

import io
import os
import sqlite3
import uuid
from datetime import datetime, date
from functools import wraps

import qrcode
from PIL import Image, ImageOps
from flask import (
    Flask, render_template, request, redirect, url_for,
    send_file, flash, abort, session, Response
)
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

DATABASE_URL = os.environ.get("DATABASE_URL")
USAR_PG = bool(DATABASE_URL)
if USAR_PG:
    import psycopg2
    import psycopg2.extras
    ErroresIntegridad = (psycopg2.IntegrityError, sqlite3.IntegrityError)
else:
    ErroresIntegridad = (sqlite3.IntegrityError,)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "mantenimiento.db")
LOGO_PATH = "/static/logo-blanco.png"

DIAS_LIMITE_INACTIVO = 365
MAX_PDF_BYTES = 8 * 1024 * 1024

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cambia-esta-clave-en-produccion")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024

ALLOWED_EXT_PDF = {"pdf"}
ALLOWED_EXT_FOTO = {"png", "jpg", "jpeg"}

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD_HASH = generate_password_hash(
    os.environ.get("ADMIN_PASSWORD", "simedic2026")
)


def allowed_file(filename, extensiones):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in extensiones


def info_estado(fecha_proximo):
    """Calcula si el mantenimiento está vigente, próximo a vencer o vencido."""
    f = datetime.strptime(fecha_proximo, "%Y-%m-%d").date()
    dias = (f - date.today()).days
    if dias < 0:
        return {"clase": "vencido", "dias": dias,
                "texto": f"Vencido hace {abs(dias)} día" + ("" if abs(dias) == 1 else "s")}
    if dias == 0:
        return {"clase": "proximo", "dias": dias, "texto": "Vence hoy"}
    if dias <= 30:
        return {"clase": "proximo", "dias": dias,
                "texto": f"Vence en {dias} día" + ("" if dias == 1 else "s")}
    return {"clase": "vigente", "dias": dias, "texto": "Vigente"}


def login_required(vista):
    @wraps(vista)
    def envoltura(*args, **kwargs):
        if not session.get("autenticado"):
            return redirect(url_for("login", siguiente=request.path))
        return vista(*args, **kwargs)
    return envoltura


# ---------------------------------------------------------------------
# Base de datos (Postgres en producción, SQLite en local)
# ---------------------------------------------------------------------
class DB:
    """Envoltura mínima para usar el mismo código con Postgres o SQLite."""

    def __init__(self):
        if USAR_PG:
            self.conn = psycopg2.connect(DATABASE_URL)
        else:
            self.conn = sqlite3.connect(DB_PATH)
            self.conn.row_factory = sqlite3.Row

    def execute(self, sql, params=()):
        if USAR_PG:
            cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql.replace("?", "%s"), params)
            return cur
        return self.conn.execute(sql, params)

    def insertar(self, sql, params=()):
        """Hace un INSERT y devuelve el id nuevo."""
        if USAR_PG:
            cur = self.execute(sql + " RETURNING id", params)
            return cur.fetchone()["id"]
        return self.execute(sql, params).lastrowid

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()


def get_db():
    return DB()


def init_db():
    pk = "SERIAL PRIMARY KEY" if USAR_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    blob = "BYTEA" if USAR_PG else "BLOB"
    db = get_db()
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS equipos (
            id {pk},
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
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS archivos (
            nombre TEXT PRIMARY KEY,
            mime TEXT NOT NULL,
            datos {blob} NOT NULL
        )
    """)
    db.execute(f"""
        CREATE TABLE IF NOT EXISTS mantenimientos (
            id {pk},
            equipo_id INTEGER NOT NULL,
            fecha_mant TEXT NOT NULL,
            fecha_proximo TEXT NOT NULL,
            tecnico TEXT NOT NULL,
            hoja_servicio_archivo TEXT,
            registrado_por TEXT,
            fecha_registro TEXT NOT NULL
        )
    """)
    # Equipos que ya existían antes de tener historial: se les crea su
    # primer registro con los datos que tenían (no se duplica si ya hay).
    db.execute("""
        INSERT INTO mantenimientos
            (equipo_id, fecha_mant, fecha_proximo, tecnico,
             hoja_servicio_archivo, registrado_por, fecha_registro)
        SELECT id, fecha_ultimo_mant, fecha_proximo_mant, tecnico,
               hoja_servicio_archivo, NULL, fecha_registro
        FROM equipos
        WHERE id NOT IN (SELECT equipo_id FROM mantenimientos)
    """)
    db.commit()
    db.close()


def registrar_mantenimiento(db, equipo_id, fecha_mant, fecha_proximo, tecnico, hoja, usuario):
    db.execute(
        """INSERT INTO mantenimientos
           (equipo_id, fecha_mant, fecha_proximo, tecnico,
            hoja_servicio_archivo, registrado_por, fecha_registro)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (equipo_id, fecha_mant, fecha_proximo, tecnico, hoja, usuario,
         datetime.now().strftime("%Y-%m-%d %H:%M")))


def historial_de(db, equipo_id, limite=10):
    return db.execute(
        """SELECT * FROM mantenimientos WHERE equipo_id = ?
           ORDER BY fecha_mant DESC, id DESC LIMIT ?""",
        (equipo_id, limite)).fetchall()


def guardar_archivo(db, nombre, mime, datos):
    binario = psycopg2.Binary(datos) if USAR_PG else sqlite3.Binary(datos)
    db.execute("INSERT INTO archivos (nombre, mime, datos) VALUES (?, ?, ?)",
               (nombre, mime, binario))


def procesar_pdf(archivo, prefijo):
    """Devuelve (nombre, bytes) del PDF o None si no es válido."""
    if not (archivo and archivo.filename and allowed_file(archivo.filename, ALLOWED_EXT_PDF)):
        return None
    datos = archivo.read()
    if not datos or len(datos) > MAX_PDF_BYTES or not datos.startswith(b"%PDF"):
        return None
    nombre = secure_filename(f"{uuid.uuid4().hex[:8]}_{prefijo}_{archivo.filename}")
    return nombre, datos


def procesar_foto(archivo, prefijo):
    """Reduce la foto (máx. 1280 px, JPEG) para que ocupe poco espacio."""
    if not (archivo and archivo.filename and allowed_file(archivo.filename, ALLOWED_EXT_FOTO)):
        return None
    try:
        img = Image.open(archivo.stream)
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((1280, 1280))
        salida = io.BytesIO()
        img.save(salida, "JPEG", quality=82, optimize=True)
    except Exception:
        return None
    nombre = secure_filename(f"{uuid.uuid4().hex[:8]}_foto_{prefijo}.jpg")
    return nombre, salida.getvalue()


def marcar_inactivos_vencidos():
    db = get_db()
    equipos = db.execute(
        "SELECT id, fecha_ultimo_mant FROM equipos WHERE activo = 1"
    ).fetchall()
    hoy = date.today()
    for eq in equipos:
        f_ultimo = datetime.strptime(eq["fecha_ultimo_mant"], "%Y-%m-%d").date()
        if (hoy - f_ultimo).days > DIAS_LIMITE_INACTIVO:
            db.execute("UPDATE equipos SET activo = 0 WHERE id = ?", (eq["id"],))
    db.commit()
    db.close()


# ---------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------
@app.route("/admin/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        usuario = request.form.get("usuario", "").strip()
        contrasena = request.form.get("contrasena", "")
        if usuario == ADMIN_USER and check_password_hash(ADMIN_PASSWORD_HASH, contrasena):
            session["autenticado"] = True
            session["usuario"] = usuario
            session.permanent = True
            siguiente = request.args.get("siguiente") or url_for("admin_lista")
            return redirect(siguiente)
        flash("Usuario o contraseña incorrectos.", "error")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def logout():
    session.pop("autenticado", None)
    return redirect(url_for("login"))


# ---------------------------------------------------------------------
# Rutas ADMIN (requieren login)
# ---------------------------------------------------------------------
@app.route("/admin")
@login_required
def admin_lista():
    marcar_inactivos_vencidos()
    q = request.args.get("q", "").strip()
    estado_f = request.args.get("estado", "").strip()
    cliente = request.args.get("cliente", "").strip()

    db = get_db()
    filas = db.execute(
        "SELECT * FROM equipos WHERE activo = 1 ORDER BY fecha_proximo_mant ASC"
    ).fetchall()
    db.close()

    resumen = {"total": 0, "vencido": 0, "proximo": 0, "vigente": 0}
    equipos = []
    for fila in filas:
        eq = dict(fila)
        eq["estado"] = info_estado(eq["fecha_proximo_mant"])
        resumen["total"] += 1
        resumen[eq["estado"]["clase"]] += 1
        if cliente and eq["numero_cliente"] != cliente:
            continue
        if estado_f in ("vencido", "proximo", "vigente") and eq["estado"]["clase"] != estado_f:
            continue
        if q:
            texto = " ".join(str(eq.get(k) or "") for k in
                             ("numero_cliente", "numero_serie", "nombre_equipo",
                              "ubicacion", "tecnico")).lower()
            if q.lower() not in texto:
                continue
        equipos.append(eq)

    return render_template("admin_lista.html", equipos=equipos, resumen=resumen,
                           q=q, estado_f=estado_f, cliente=cliente)


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

        pdf = procesar_pdf(request.files.get("hoja_servicio"), numero_cliente)
        foto = procesar_foto(request.files.get("foto_equipo"), numero_cliente)

        db = get_db()
        try:
            if pdf:
                guardar_archivo(db, pdf[0], "application/pdf", pdf[1])
            if foto:
                guardar_archivo(db, foto[0], "image/jpeg", foto[1])
            nuevo_id = db.insertar(
                """INSERT INTO equipos
                   (numero_cliente, numero_serie, nombre_equipo, ubicacion,
                    fecha_ultimo_mant, fecha_proximo_mant, tecnico,
                    hoja_servicio_archivo, foto_equipo, activo, fecha_registro)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (numero_cliente, numero_serie, nombre_equipo, ubicacion,
                 fecha_ultimo, fecha_proximo, tecnico,
                 pdf[0] if pdf else None, foto[0] if foto else None,
                 date.today().isoformat())
            )
            registrar_mantenimiento(db, nuevo_id, fecha_ultimo, fecha_proximo, tecnico,
                                    pdf[0] if pdf else None, session.get("usuario"))
            db.commit()
        except ErroresIntegridad:
            db.close()
            flash("Ese número de serie ya está registrado en otro equipo.", "error")
            return redirect(url_for("admin_nuevo"))
        db.close()

        flash("Equipo registrado correctamente. Ya puedes descargar su QR o copiar su enlace NFC.", "ok")
        return redirect(url_for("admin_lista"))

    return render_template("admin_nuevo.html")


@app.route("/admin/editar/<int:equipo_id>", methods=["GET", "POST"])
@login_required
def admin_editar(equipo_id):
    db = get_db()
    equipo = db.execute("SELECT * FROM equipos WHERE id = ?", (equipo_id,)).fetchone()
    if equipo is None:
        db.close()
        abort(404)

    if request.method == "POST":
        fecha_ultimo = request.form["fecha_ultimo_mant"]
        fecha_proximo = request.form["fecha_proximo_mant"]
        tecnico = request.form["tecnico"].strip()

        nombre_pdf = equipo["hoja_servicio_archivo"]
        pdf = procesar_pdf(request.files.get("hoja_servicio"), equipo["numero_cliente"])
        if pdf:
            guardar_archivo(db, pdf[0], "application/pdf", pdf[1])
            nombre_pdf = pdf[0]

        nombre_foto = equipo["foto_equipo"]
        foto = procesar_foto(request.files.get("foto_equipo"), equipo["numero_cliente"])
        if foto:
            guardar_archivo(db, foto[0], "image/jpeg", foto[1])
            nombre_foto = foto[0]

        registrar_mantenimiento(db, equipo_id, fecha_ultimo, fecha_proximo, tecnico,
                                pdf[0] if pdf else None, session.get("usuario"))
        db.execute(
            """UPDATE equipos SET fecha_ultimo_mant = ?, fecha_proximo_mant = ?,
               tecnico = ?, hoja_servicio_archivo = ?, foto_equipo = ?, activo = 1
               WHERE id = ?""",
            (fecha_ultimo, fecha_proximo, tecnico, nombre_pdf, nombre_foto, equipo_id)
        )
        db.commit()
        db.close()
        flash("Mantenimiento actualizado. El QR/NFC pegado en el equipo sigue funcionando igual.", "ok")
        return redirect(url_for("admin_lista"))

    historial = historial_de(db, equipo_id)
    db.close()
    return render_template("admin_editar.html", equipo=equipo, historial=historial)


@app.route("/admin/eliminar/<int:equipo_id>", methods=["POST"])
@login_required
def admin_eliminar(equipo_id):
    db = get_db()
    db.execute("UPDATE equipos SET activo = 0 WHERE id = ?", (equipo_id,))
    db.commit()
    db.close()
    flash("Equipo eliminado del listado de clientes.", "ok")
    return redirect(url_for("admin_lista"))


@app.route("/admin/qr/<int:equipo_id>")
@login_required
def descargar_qr(equipo_id):
    """Genera el QR al momento (así siempre trae el dominio correcto)."""
    url_publica = url_for("ver_equipo", equipo_id=equipo_id, _external=True)
    salida = io.BytesIO()
    qrcode.make(url_publica).save(salida, "PNG")
    salida.seek(0)
    return send_file(salida, mimetype="image/png", as_attachment=True,
                     download_name=f"equipo_{equipo_id}.png")


# ---------------------------------------------------------------------
# Rutas PÚBLICAS (sin login): ficha del equipo y sus archivos
# ---------------------------------------------------------------------
@app.route("/equipo/<int:equipo_id>")
def ver_equipo(equipo_id):
    db = get_db()
    equipo = db.execute(
        "SELECT * FROM equipos WHERE id = ? AND activo = 1", (equipo_id,)
    ).fetchone()
    if equipo is None:
        db.close()
        abort(404)
    historial = historial_de(db, equipo_id, 6)
    db.close()

    return render_template(
        "equipo_publico.html", equipo=equipo, logo=LOGO_PATH,
        estado=info_estado(equipo["fecha_proximo_mant"]), historial=historial
    )


@app.route("/static/uploads/<path:filename>")
def archivo_subido(filename):
    db = get_db()
    fila = db.execute("SELECT mime, datos FROM archivos WHERE nombre = ?", (filename,)).fetchone()
    db.close()
    if fila is None:
        abort(404)
    return Response(bytes(fila["datos"]), mimetype=fila["mime"],
                    headers={"Cache-Control": "public, max-age=86400"})


@app.errorhandler(404)
def no_encontrado(e):
    return render_template("404.html"), 404


init_db()

if __name__ == "__main__":
    puerto = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=puerto, debug=True)
