# Sistema Taller & Rentacar

Aplicación Flask para separar ingresos de taller y rentas, registrar gastos, emitir reportes y calcular la comisión del 1 % sobre trabajos de taller y facturas pagadas. El total neto diario es rentas + taller − gastos. Usa SQLite localmente y PostgreSQL cuando se configura `DATABASE_URL`.

## Inicio rápido

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Abra `http://127.0.0.1:5000`.

Usuario inicial: `admin`  
Contraseña inicial: `admin123`

Cambie la contraseña inicial antes de utilizar el sistema con datos reales. Configure una variable de entorno `SECRET_KEY` para producción y ejecute la aplicación con un servidor WSGI, no con `debug=True`.

## PostgreSQL y hosting

La aplicación usa PostgreSQL automáticamente cuando existe la variable `DATABASE_URL`:

```powershell
$env:DATABASE_URL="postgresql://usuario:contraseña@servidor:5432/taller_rentacar"
$env:SECRET_KEY="una-clave-larga-y-privada"
gunicorn app:app
```

Para copiar los datos que ya existen en SQLite a una base PostgreSQL nueva:

```powershell
$env:DATABASE_URL="postgresql://usuario:contraseña@servidor:5432/taller_rentacar"
python migrate_to_postgres.py
```

El script se detiene si el destino contiene datos para evitar sobrescrituras. Para hosting, configure también `DATA_DIR` en almacenamiento persistente si desea conservar los CSV que se generan al cerrar el día. La base de datos es la fuente oficial de todos los ingresos, cierres y pagos.
