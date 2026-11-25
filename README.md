# OroCopy

Proyecto paralelo basado en OroControl. Conserva el panel nativo de MT5 y añade un lector visual del contador COMPRAS/VENTAS de un directo, con copia opcional protegida para cuentas DEMO.

## Estado actual

- `Ctrl+Alt+G`: oculta la barra de Windows y muestra el panel en ese mismo rectángulo; al pulsarlo de nuevo o presionar `Esc`, restaura la barra.
- `Ctrl+Alt+Q`: cierra OroCopy y restaura la barra.
- `Ctrl+Alt+O`: abre el lector visual compacto de TikTok. Si se inicia el lector por separado, este inicia también el dashboard si todavía no está abierto.
- El menú del icono en la bandeja permite abrir OroCopy, el lector, ocultar/restaurar MT5 y salir. Ocultar MT5 mantiene su proceso ejecutándose.
- La cotización y las órdenes se leen/envían por el paquete oficial `MetaTrader5` de Python, conectado al terminal local.
- El bloque `OPERACIÓN` muestra el sentido, si va ganando o perdiendo y el P/L flotante actualizado desde MT5 aproximadamente cada segundo. El precio en vivo cambia a verde con P/L total positivo y a rojo con P/L negativo; debajo aparece el precio medio de entrada BUY/SELL. Ya no se muestran bid, ask ni spread.
- Los campos visibles del dashboard son símbolo y volumen; no se guardan contraseñas ni tokens. No se muestran campos SL/TP ni parámetros del modo crucero.
- El estado superior muestra DEMO/REAL y conexión; sin MT5 conectado, bloquea las acciones de trading.
- El estado de protección muestra si la posición tiene SL y TP. Antes de abrir, estima la pérdida al SL con `order_calc_profit` si se ingresó un SL; el cálculo es aproximado y puede variar por ejecución y costos del bróker.
- El estado de protección indica si las posiciones abiertas tienen SL y TP; avisa en rojo/ámbar si falta alguno.
- BUY o SELL envía diez órdenes a mercado de forma secuencial, cada una con el lote configurado. Por tanto, el volumen total solicitado es diez veces el lote. Puede haber ejecución parcial si el terminal o bróker rechaza alguna orden; el panel indica cuántas se aceptaron.
- Las órdenes manuales BUY/SELL no llevan SL/TP inicial. El dashboard conserva BE y los controles de cierre para gestionar las posiciones abiertas.
- BE mueve el SL al precio de entrada de todas las posiciones. El botón `50% + BE` cierra la mitad del volumen y, si completa el parcial, mueve a breakeven el SL de las posiciones que quedan abiertas. `CERRAR 100%` intenta cerrar toda la exposición.
- Los controles dependen del estado: sin posiciones, BUY/SELL están disponibles y los controles de gestión se desactivan; al detectar una posición, BUY/SELL se bloquean y BE y los cierres quedan disponibles.
- El botón COPIAR/PAUSAR de la ventana del lector controla el conteo estable. COPIAR arma la sincronización al pulsarlo, sin popup de confirmación; solo envía órdenes si MT5 verifica una cuenta DEMO. Usa el símbolo y lote guardados en la configuración del dashboard. Las posiciones copiadas usan un magic propio y se sincronizan por cantidad.
- Al reducirse el conteo observado, OroCopy cierra los tickets excedentes y mueve los restantes a BE; un conteo cero cierra todas las posiciones de OroCopy. La copia ya no se pausa automáticamente por fallos de OCR y no tiene un máximo fijo de posiciones por sentido. Si la lectura permanece dudosa o sin fotogramas durante 60 segundos consecutivos, primero intenta cerrar directamente las posiciones copiadas del símbolo; si MT5 rechaza un cierre, mueve a BE las posiciones que sigan abiertas. La copia continúa armada; si también falla el BE, lo indica para revisión en MT5. Tras un cierre correcto o protección en BE, mantiene cerrada la última señal hasta que cambie el conteo confirmado. Si una señal nueva se confirma durante el cierre, la sincroniza después. PAUSAR sigue siendo manual.
- El contador del dashboard muestra los tickets copiados BUY, SELL y el total cerrado durante la sesión del observador. BE y los botones de cierre del dashboard actúan sobre todas las posiciones del símbolo, incluidas las copiadas.

## Requisitos

- Windows 10/11.
- Python 3.12 de 64 bits para el conector de MetaTrader 5.
- `pystray` y `Pillow` para el icono y el menú de control en la bandeja.
- Terminal MetaTrader 5 instalado, abierto y conectado a la cuenta del bróker.
- Prepara el entorno una sola vez desde PowerShell:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\setup.ps1
```

## Inicio

Desde PowerShell, en esta carpeta:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\start.ps1
```

El panel no se abre solo: inicia en segundo plano y espera `Ctrl+Alt+G`. No se instala como servicio ni arranca con Windows.

## Lector visual del directo

- Inicia OroCopy y pulsa `Ctrl+Alt+O` (o selecciona `Lector de TikTok` desde el menú del icono en la bandeja). El lector inicia el dashboard si aún no estaba abierto.
- Para capturar desde monitor, `ZONA` pide elegir la pantalla y luego seleccionar por separado COMPRAS y VENTAS. En cada recuadro incluye la fila `POSICIONES` y su número; no hace falta incluir los títulos COMPRAS/VENTAS. `GUARDAR PLANTILLA` conserva las dos zonas y `PLANTILLA` las vuelve a cargar. Las plantillas antiguas de una sola franja se separan inicialmente en dos mitades (VENTAS a la izquierda, COMPRAS a la derecha); ajusta ambas zonas si hace falta. OroCopy valida cada recorte dentro de su monitor.
- Pulsa `Iniciar lectura`. La ventana compacta muestra ambas zonas y tres lecturas OCR (gris con autocontraste, umbral adaptativo local y detalle con enfoque), además de la confianza de los dígitos. COMPRAS y VENTAS se procesan en paralelo. Un conteo debe repetirse en tres fotogramas consecutivos, capturados aproximadamente cada 0,45 segundos, antes de confirmar la señal. Si las variantes reconocen valores distintos o la confianza es baja, se conserva el último conteo confirmado y no se sincronizan órdenes por esa lectura dudosa.
- En la ventana del lector, pulsa COPIAR para iniciar y PAUSAR para detener. La pausa no liquida posiciones abiertas.
- Los cambios estables se guardan en `%APPDATA%\OroCopy\screen_observer.jsonl`; las coordenadas en `%APPDATA%\OroCopy\observer.json`; las sincronizaciones en `%APPDATA%\OroCopy\copy_execution.jsonl`.
- Requiere Tesseract OCR para Windows además de las dependencias Python instaladas por `setup.ps1`. Sin Tesseract, la vista previa funciona, pero no la lectura del texto.
- El registro marca cuándo el cambio apareció en pantalla; no conoce el retraso del directo respecto a la operación real del experto.

## Captura de una pestaña Chrome

- En el lector local, elige `Pestaña Chrome`, pulsa `Vincular Chrome` y pega el token copiado en la extensión.
- En `chrome://extensions`, activa el modo desarrollador, pulsa `Cargar descomprimida` y elige la carpeta `chrome_extension` de este proyecto. Si ya estaba cargada, pulsa Recargar para activar el selector doble.
- Abre la pestaña del directo, pulsa el icono OroCopy y elige `Seleccionar zonas COMPRAS/VENTAS`. Arrastra primero alrededor de la fila `POSICIONES` de COMPRAS con su número y luego alrededor de la misma fila de VENTAS. Los títulos superiores pueden quedar tapados porque cada zona ya tiene su lado asignado.
- En el lector local pulsa `Iniciar lectura`; luego, desde la extensión, pulsa `Capturar esta pestaña` y acepta el permiso de captura de Chrome.
- La extensión recorta ambas zonas desde el flujo de video de esa pestaña y las envía juntas como un JPEG cada 0,65 s al proceso OroCopy en `127.0.0.1`; el lector procesa COMPRAS y VENTAS de forma independiente. Los recortes y OCR siguen funcionando si otra ventana cubre Chrome; no minimices ni cierres la pestaña durante la captura.
- El token local está en `%APPDATA%\\OroCopy\\chrome_capture_token.txt` y no se incluye en Git. El puente escucha solo en `127.0.0.1:39872`, acepta llamadas desde la extensión con token válido y recibe imágenes; las órdenes de trading siguen protegidas por la lógica demo de OroCopy.
- Para detener, pulsa `Detener` en la extensión y `Detener lectura` en OroCopy. La pausa de la copia conserva las posiciones en MT5, igual que en el modo monitor.

## Vista previa sin MT5

Para ver la barra antes de instalar Python o MT5, abre una PowerShell interactiva en esta carpeta y ejecuta:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\preview.ps1
```

La vista previa ocupa el rectángulo detectado de la barra de Windows, ajusta el ancho y el tamaño de controles al espacio disponible y distribuye los elementos con más separación cuando la barra es alta. Muestra la fecha y hora local en formato `dd/MM/yyyy` y `HH:mm:ss`. La cotización queda vacía y los controles de operación permanecen desactivados. En la vista previa, `Esc` o `Ctrl+Alt+G` devuelve la barra de Windows; `Ctrl+Alt+Q` cierra la vista previa.

## Uso previsto

1. Abre MetaTrader 5 y confirma que la cuenta demo esté conectada y que `XAUUSDm` esté disponible. Ese es el símbolo inicial que carga OroCopy; el campo PAR sigue editable.
2. Inicia OroCopy y pulsa `Ctrl+Alt+G`.
3. Ajusta símbolo y volumen. BUY/SELL envían diez órdenes a mercado al primer clic, sin SL/TP inicial.
4. Con posiciones abiertas, BE mueve el SL al precio de entrada; `50% + BE` cierra la mitad y protege las restantes; CERRAR 100% intenta cerrar todo.
5. Para copiar, inicia la lectura, espera un conteo estable, pulsa COPIAR en el lector y confirma DEMO, símbolo, lote y conteo. PAUSAR detiene futuras sincronizaciones, pero no cierra las posiciones restantes.

## Recuperar la barra de Windows

Normalmente se restaura al ocultar el panel, cerrar la app o presionar `Ctrl+Alt+Q`. Si se fuerza el cierre del proceso mientras la barra está oculta, ejecuta:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\restore_taskbar.ps1
```

## Precauciones

La copia visual puede ir retrasada respecto al directo. Revisa en MT5 cada orden y no uses una cuenta real para validar el OCR. Las órdenes copiadas se envían sin SL/TP inicial; la pausa del lector no cierra posiciones. No se deben introducir credenciales en OroCopy: el login se mantiene dentro de MT5.
