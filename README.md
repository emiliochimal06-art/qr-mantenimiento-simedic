# Sistema de QR de mantenimiento preventivo — SIMEDIC

Reemplaza las etiquetas impresas por un QR que **no cambia nunca**: apunta
siempre a `tudominio.com/equipo/<id>`, y esa página lee los datos más
recientes desde la base de datos. Actualizar un mantenimiento = editar el
registro, no reimprimir el QR.

## Instalación

```bash
python -m venv venv
source venv/bin/activate      # en Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Abre `http://127.0.0.1:5000/admin`.

## Uso

1. **Registrar equipo** (`/admin/nuevo`): captura número de cliente, equipo,
   fechas de mantenimiento, técnico y sube la hoja de servicio (PDF). Al
   guardar se genera automáticamente el QR (`/admin/qr/<id>`) — ese es el
   que se imprime y pega en el equipo, una sola vez.
2. **Cada mantenimiento nuevo**: entra a "Actualizar" en el equipo
   correspondiente, cambia fechas/técnico y sube la nueva hoja de servicio.
   El QR físico sigue funcionando igual porque la URL no cambió.
3. **Vencimiento automático**: si pasan más de 365 días desde el último
   mantenimiento sin renovarse, el equipo se marca inactivo y desaparece
   del listado de clientes (no se borra el historial, solo se oculta).

## Antes de imprimir los QR definitivos

Cambia `BASE_URL` en `app.py` por el dominio real donde publiques la app
(ej. `https://simedic.mx`). Si generas los QR con `127.0.0.1` y luego
cambias de dominio, esos QR dejan de servir y sí tendrías que reimprimir.

## Poner tu logo

Coloca tu logo como `static/logo.png` — aparece automáticamente en la
ficha pública que ve el cliente al escanear.

## Siguientes pasos sugeridos para la estadía

- Desplegar en un hosting gratuito/barato (Render, PythonAnywhere, Railway)
  para tener una URL pública real.
- Opcional: generar también el PDF descargable de la ficha (con
  `reportlab` o `weasyprint`) si el profesor/empresa lo prefiere sobre
  la página web.
- Opcional: autenticación simple en `/admin` (usuario/contraseña) antes
  de presentarlo, para que no cualquiera pueda editar los registros.
