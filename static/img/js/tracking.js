/*
 * Tracking liviano de tiempo-en-página y clicks en botones clave.
 *
 * - Tiempo en página: mide cuánto tiempo la pestaña estuvo VISIBLE en esta
 *   página (se pausa si el visitante cambia de pestaña o minimiza) y lo
 *   manda cuando se va (cambia de pestaña, cierra, o navega a otra página).
 * - Clicks: cualquier elemento con el atributo data-track="algo" manda un
 *   evento "click" con ese nombre apenas lo tocan -- no hace falta escribir
 *   nada de JS por botón, solo agregar el atributo en el HTML, ej:
 *     <button data-track="cta_registrarse">Probar gratis</button>
 *
 * Usa sendBeacon (con fallback a fetch keepalive) para que el envío no
 * demore ni bloquee la navegación, y para que funcione incluso si el
 * visitante ya está cerrando la pestaña.
 */
(function () {
  "use strict";

  var ENDPOINT = "/api/evento-web";

  function enviar(payload) {
    try {
      var body = JSON.stringify(payload);
      if (navigator.sendBeacon) {
        var blob = new Blob([body], { type: "application/json" });
        navigator.sendBeacon(ENDPOINT, blob);
      } else {
        fetch(ENDPOINT, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: body,
          keepalive: true,
        }).catch(function () {});
      }
    } catch (e) {
      /* si esto falla, no debe romper nada de la página */
    }
  }

  // ---------- Tiempo en página ----------
  var inicioVisible = document.visibilityState === "visible" ? Date.now() : null;
  var acumuladoMs = 0;
  var enviado = false;

  function pausar() {
    if (inicioVisible !== null) {
      acumuladoMs += Date.now() - inicioVisible;
      inicioVisible = null;
    }
  }

  function reanudar() {
    if (document.visibilityState === "visible" && inicioVisible === null) {
      inicioVisible = Date.now();
    }
  }

  function enviarTiempo() {
    if (enviado) return;
    pausar();
    var segundos = Math.round(acumuladoMs / 1000);
    if (segundos < 1) return; // menos de 1s no aporta nada y son la mayoría de los bots
    enviado = true;
    enviar({ tipo: "tiempo_en_pagina", ruta: location.pathname, valor_seg: segundos });
  }

  document.addEventListener("visibilitychange", function () {
    if (document.visibilityState === "hidden") {
      pausar();
    } else {
      reanudar();
    }
  });

  // pagehide cubre navegar a otra página Y cerrar la pestaña; es más
  // confiable que beforeunload en mobile (Safari/Chrome iOS lo ignoran a
  // veces cuando la página queda en el historial "back-forward cache").
  window.addEventListener("pagehide", enviarTiempo);
  window.addEventListener("beforeunload", enviarTiempo);

  // ---------- Clicks en botones marcados con data-track ----------
  document.addEventListener(
    "click",
    function (ev) {
      var el = ev.target.closest("[data-track]");
      if (!el) return;
      enviar({ tipo: "click", nombre: el.getAttribute("data-track"), ruta: location.pathname });
    },
    true
  );
})();
