CRAFT AND BEER - SISTEMA DE VENTAS ONLINE
Notas rápidas de cómo funciona la página

------------------------------------------------------
QUÉ ES
------------------------------------------------------
Es una página web (un solo archivo HTML) que simula el sistema de
ventas online de la cervecería Craft and Beer. La idea es que los
clientes puedan comprar cervezas en botella o growler desde internet,
pagar por una plataforma externa, recibir su boleta digital y que el
local pueda organizar el despacho a domicilio.

No tiene servidor ni base de datos real: todo se guarda en el
localStorage del navegador, así que los datos quedan guardados en el
mismo computador donde se abre, pero no se comparten entre distintos
dispositivos ni personas.

------------------------------------------------------
CÓMO SE ENTRA
------------------------------------------------------
Al abrir la página aparece una pantalla de login con dos pestañas:

- "Soy cliente": para clientes que ya se registraron o que se quieren
  registrar por primera vez.
- "Soy personal": para el personal del local (administrador, cajero
  virtual, encargado de despacho y dueño).

Cuentas de demostración para el personal:
  admin / admin3604         -> Administrador
  cajero / cajero3604       -> Cajero Virtual
  despacho / despacho3604   -> Encargado de Despacho
  dueno / dueno3604         -> Dueño

Como cliente puedes registrarte tú mismo con tus datos, o entrar
directo con la cuenta de prueba: javiera.soto@demo.cl

------------------------------------------------------
LO QUE VE UN CLIENTE
------------------------------------------------------
- Catálogo: lista de cervezas artesanales (botellas y growlers) con
  estilo, grado alcohólico, precio y stock disponible. Se pueden
  agregar unidades directo desde ahí.

- Mi pedido: es el carrito de compras. Muestra lo agregado, el total,
  y permite elegir la dirección de despacho y el medio de pago
  (Servipag o depósito bancario). Al pagar, se genera la venta, se
  descuenta el stock y se envía la boleta digital (simulada) al
  correo del cliente.

- Mis compras: historial de pedidos del cliente, con su estado
  (Pagado, En despacho, Entregado o Anulado) y la última boleta
  digital en formato de recibo. Desde aquí también se puede anular
  una compra indicando el motivo, mientras no esté ya despachada.

- Mi cuenta: muestra los datos personales registrados (RUN, dirección,
  comuna, correo, teléfono, etc.)

------------------------------------------------------
LO QUE VE EL PERSONAL
------------------------------------------------------
Administrador:
  - Mantenedor de productos: editar precio y stock, o agregar
    cervezas nuevas al catálogo.
  - Mantenedor de clientes: ver la lista de clientes y registrar uno
    nuevo directamente desde el local.
  - Mantenedor de usuarios: crear cuentas nuevas de personal y
    asignarles un rol.
  - Anular compra: puede anular cualquier venta pagada o en
    despacho a solicitud del cliente.

Cajero Virtual:
  - Ve las ventas que quedan asociadas a su caja (las que se pagaron
    por la web) y el historial completo de ventas con su estado.

Encargado de Despacho:
  - Ve los pedidos pendientes por preparar, en el orden en que
    llegaron.
  - Puede imprimir la orden de despacho (abre una ventana lista para
    imprimir).
  - Marca los pedidos como "en ruta" y luego como "entregado".

Dueño:
  - Reporte de ventas: elige un rango de fechas y ve cuántas ventas
    hubo, cuántas unidades se vendieron, el total de ingresos y el
    detalle de ventas por producto.
  - También puede revisar el mantenedor de productos.

------------------------------------------------------
COSAS A TENER EN CUENTA
------------------------------------------------------
- El pago externo (Servipag / depósito) y el envío de correos son
  simulados: no se conecta a ningún servicio real, solo confirma al
  tiro para que se pueda ver el flujo completo.
- El stock se descuenta al pagar y se repone si la compra se anula.
- Al cerrar sesión y volver a entrar, los datos siguen ahí porque
  quedaron guardados en el navegador (localStorage), a menos que se
  borre el historial o se abra desde otro computador.
- Si algo se ve raro o se pierde el estado, se puede limpiar borrando
  los datos de sitio del navegador para esa página y se reinicia todo
  desde cero (vuelve el catálogo original de demo).
