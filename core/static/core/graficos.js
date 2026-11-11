// Gráficos del panel de Inicio: barras en SVG, sin librerías externas.
(function () {
  const datos = document.getElementById("graficos-datos");
  if (!datos) return;
  const g = JSON.parse(datos.textContent);
  const NS = "http://www.w3.org/2000/svg";
  const pesos = (n) => (n < 0 ? "−$" : "$") + Math.abs(Math.round(n)).toLocaleString("es-CL");
  const compacto = (n) => {
    const a = Math.abs(n);
    if (a >= 1e6) return "$" + (n / 1e6).toLocaleString("es-CL", { maximumFractionDigits: 1 }) + " M";
    if (a >= 1e3) return "$" + Math.round(n / 1e3).toLocaleString("es-CL") + " mil";
    return "$" + Math.round(n).toLocaleString("es-CL");
  };
  const el = (tag, attrs, padre) => {
    const e = document.createElementNS(NS, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    if (padre) padre.appendChild(e);
    return e;
  };
  const tip = document.createElement("div");
  tip.className = "grafico-tip";
  document.body.appendChild(tip);
  function mostrar(evento, html) {
    tip.innerHTML = html;
    tip.style.display = "block";
    const r = tip.getBoundingClientRect();
    let x = evento.clientX + 14, y = evento.clientY - r.height - 10;
    if (x + r.width > innerWidth - 8) x = evento.clientX - r.width - 14;
    if (y < 8) y = evento.clientY + 16;
    tip.style.left = x + "px"; tip.style.top = y + "px";
  }
  const ocultar = () => { tip.style.display = "none"; };

  function escala(maximo, minimo) {
    const rango = Math.max(maximo, 0) - Math.min(minimo, 0) || 1;
    const bruto = rango / 4, pot = Math.pow(10, Math.floor(Math.log10(bruto)));
    const paso = [1, 2, 2.5, 5, 10].map((m) => m * pot).find((p) => p >= bruto);
    return { arriba: Math.ceil(Math.max(maximo, 0) / paso) * paso || paso,
             abajo: Math.floor(Math.min(minimo, 0) / paso) * paso, paso };
  }

  // Columnas verticales (una o dos series), con eje y grilla discreta.
  function columnas(contenedor, etiquetas, series, rangos) {
    const W = contenedor.clientWidth || 600, H = 240, izq = 64, der = 8, arr = 12, aba = 28;
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", height: H, role: "img" });
    const todos = series.flatMap((s) => s.valores);
    const e = escala(Math.max(...todos), Math.min(...todos));
    const y = (v) => arr + (e.arriba - v) / (e.arriba - e.abajo) * (H - arr - aba);
    for (let v = e.abajo; v <= e.arriba + 1e-6; v += e.paso) {
      el("line", { x1: izq, x2: W - der, y1: y(v), y2: y(v), class: v === 0 ? "eje" : "grilla" }, svg);
      el("text", { x: izq - 8, y: y(v) + 4, "text-anchor": "end", class: "eje-texto" }, svg).textContent = compacto(v);
    }
    const n = etiquetas.length, banda = (W - izq - der) / n;
    const ancho = Math.min(24, (banda * 0.7) / series.length - 2);
    const paso = Math.ceil(n / Math.floor((W - izq) / 48));
    etiquetas.forEach((et, i) => {
      const cx = izq + banda * (i + 0.5);
      if ((n - 1 - i) % paso === 0)  // se cuenta desde el período actual, que siempre se rotula
        el("text", { x: cx, y: H - 8, "text-anchor": "middle", class: "eje-texto" }, svg).textContent = et;
      series.forEach((s, k) => {
        const v = s.valores[i], x = cx - (series.length * (ancho + 2)) / 2 + k * (ancho + 2) + 1;
        const y0 = y(0), y1 = y(v), alto = Math.abs(y1 - y0);
        if (alto > 0.5) {
          const r = Math.min(4, alto, ancho / 2), arriba = v >= 0, top = Math.min(y0, y1);
          const d = arriba
            ? `M${x},${y0}V${top + r}Q${x},${top} ${x + r},${top}H${x + ancho - r}Q${x + ancho},${top} ${x + ancho},${top + r}V${y0}Z`
            : `M${x},${y0}V${y1 - r}Q${x},${y1} ${x + r},${y1}H${x + ancho - r}Q${x + ancho},${y1} ${x + ancho},${y1 - r}V${y0}Z`;
          el("path", { d, fill: s.color, class: "barra" }, svg);
        }
      });
      const zona = el("rect", { x: izq + banda * i, y: arr, width: banda, height: H - arr - aba, class: "zona" }, svg);
      const html = `<strong>${rangos[i]}</strong>` + series.map((s) =>
        `<div><span class="muestra" style="background:${s.color}"></span>${s.nombre}: <b>${pesos(s.valores[i])}</b></div>`).join("");
      zona.addEventListener("mousemove", (ev) => mostrar(ev, html));
      zona.addEventListener("mouseleave", ocultar);
    });
    contenedor.innerHTML = "";
    contenedor.appendChild(svg);
  }

  // Barras horizontales con el valor al final.
  function barras(contenedor, filas, color) {
    if (!filas.length) { contenedor.innerHTML = '<p class="vacio">Sin datos en este período.</p>'; return; }
    const W = contenedor.clientWidth || 400, fila = 34, H = filas.length * fila + 4, izq = 132, der = 96;
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", height: H, role: "img" });
    const max = Math.max(...filas.map((f) => f.valor), 1);
    filas.forEach((f, i) => {
      const yc = i * fila + fila / 2, largo = Math.max((W - izq - der) * f.valor / max, f.valor > 0 ? 3 : 0);
      const nombre = f.nombre.length > 18 ? f.nombre.slice(0, 17) + "…" : f.nombre;
      el("text", { x: izq - 10, y: yc + 4, "text-anchor": "end", class: "eje-texto fuerte" }, svg).textContent = nombre;
      if (largo > 0) {
        const r = Math.min(4, largo), x = izq, y0 = yc - 9, h = 18;
        el("path", { d: `M${x},${y0}H${x + largo - r}Q${x + largo},${y0} ${x + largo},${y0 + r}V${y0 + h - r}Q${x + largo},${y0 + h} ${x + largo - r},${y0 + h}H${x}Z`,
                     fill: f.color || color, class: "barra" }, svg);
      }
      el("text", { x: izq + largo + 8, y: yc + 4, class: "eje-texto fuerte" }, svg).textContent = compacto(f.valor);
      const zona = el("rect", { x: 0, y: i * fila, width: W, height: fila, class: "zona" }, svg);
      zona.addEventListener("mousemove", (ev) => mostrar(ev, `<strong>${f.nombre}</strong><div>${pesos(f.valor)}</div>`));
      zona.addEventListener("mouseleave", ocultar);
    });
    contenedor.innerHTML = "";
    contenedor.appendChild(svg);
  }

  const VERDE = "#2f8a55", AZUL = "#2a78d6";
  function dibujar() {
    const q = (id) => document.getElementById(id);
    if (q("g-ventas")) columnas(q("g-ventas"), g.etiquetas, [{ nombre: "Ventas netas", valores: g.ventas, color: VERDE }], g.rangos);
    if (q("g-cobros")) columnas(q("g-cobros"), g.etiquetas, [
      { nombre: "Facturado", valores: g.facturado, color: VERDE },
      { nombre: "Cobrado", valores: g.cobrado, color: AZUL }], g.rangos);
    if (q("g-clientes")) barras(q("g-clientes"), g.clientes, VERDE);
    if (q("g-antiguedad")) barras(q("g-antiguedad"),
      g.antiguedad.etiquetas.map((nombre, i) => ({ nombre, valor: g.antiguedad.valores[i] })), AZUL);
  }
  dibujar();
  let espera;
  addEventListener("resize", () => { clearTimeout(espera); espera = setTimeout(dibujar, 150); });
})();
