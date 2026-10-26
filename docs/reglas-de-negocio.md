# Reglas de negocio

Este documento explica **por qué** el servidor calcula las cosas como las calcula. Cada regla salió de comparar los resultados del MCP con el cierre mensual que se hacía a mano, hasta que cuadraron.

Todas se configuran en `config/business_rules.json`.

## 1. Lead del mes

Un lead pertenece al mes en que se **cargó** en GoContact (informe *Contact state by last load date*).

- Solo se cuentan las bases de proveedores (`bases_leads`).
- El informe puede repetir un mismo Contact ID: se deja una fila por contacto.
- La columna *DB Created Date* es la fecha de creación de la **base**, no del lead. Si el informe no trae la fecha de carga, se pide a `GET /api/contacts/`.

## 2. Proveedor

El campo `proveedor` del contacto llega sin normalizar (el mismo proveedor escrito de varias formas, valores numéricos sueltos, etc.). Por eso:

1. **Manda el id de formulario.** Se busca en él el texto de cada proveedor (`proveedores`).
2. Si hay id de formulario pero no coincide con ningún proveedor → *Otros / propios*.
3. Solo si **no** hay id de formulario se usa el campo `proveedor`, con sus alias (`proveedor_alias`).

Para las ventas, el id de formulario se busca en cascada: campo del contacto → id del lead (por Contact ID) → origen de la llamada saliente → origen de la llamada entrante. Los orígenes marcados como no válidos se ignoran.

## 3. Venta segura

Una llamada cuenta como venta solo si en el script se cumplen **las tres** condiciones:

| Campo del script | Debe estar en |
|---|---|
| Grupo | `venta.grupo` |
| Final (producto) | `venta.productos` |
| Gestión realizada | `venta.gestiones` |

Se consideran tanto campañas (salientes) como colas (entrantes): muchas ventas entran por llamada entrante.

Los criterios se pueden sobrescribir en cada consulta (`ventas_mes` acepta `grupo`, `productos` y `gestiones`).

## 4. Llamada contestada

Contestada = outcome que **no** está en `outcomes_no_contestada` **y** tiempo de conversación > 0.

El tiempo de conversación por sí solo no sirve: en la validación, casi la mitad de las llamadas "No Contactado" tenían segundos de conversación (buzones de voz, locuciones, cuelgues).

## 5. Tramos de llamada

En colas, una misma llamada (UUID) puede tener varios tramos por transferencias entre agentes o colas. Se conservan todos, porque cada tramo es trabajo de un agente. Solo se eliminan las filas idénticas (mismo UUID, agente, owner y hora de inicio).

## 6. Campañas y colas de servicio

Una campaña o cola es de "servicio" si su nombre empieza por alguno de `servicio.empieza_por` o coincide exactamente con alguno de `servicio.exacto` (sin distinguir mayúsculas). Las campañas nuevas que sigan la nomenclatura entran solas.

## Mantenimiento

Revisa estas listas cuando:

- se cree un producto, una gestión de venta o un outcome nuevo en el script;
- entre un proveedor nuevo o cambie la nomenclatura de sus formularios;
- se creen campañas o colas con nombres que no sigan el patrón.

Si una lista se queda desactualizada, los números se desvían **sin avisar**. Una buena práctica es comparar cada mes el total de ventas del MCP con el cierre oficial.
