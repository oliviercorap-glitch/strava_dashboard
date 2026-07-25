# Front — saisie ressenti + débrief inline dans `dashboard.html`

Ton `/api/data` renvoie déjà `recentes` (liste d'activités). Après le patch 5,
chaque activité de cette liste porte aussi `ressenti` et `debrief`.

L'idée : dans le rendu de chaque activité récente, ajouter un petit bloc
"coach" repliable. Adapte les noms de fonctions/conteneurs à ton JS existant.

---

## HTML/JS — bloc à insérer dans le rendu d'une activité

```html
<!-- À placer dans le template/rendu de chaque activité de la liste "recentes" -->
<div class="coach-block" data-activity-id="${a.id}">
  ${a.debrief ? `
    <div class="debrief-affiche">
      <div class="debrief-label">🎯 Débrief coach</div>
      <div class="debrief-texte">${a.debrief.replace(/\n/g, '<br>')}</div>
      ${a.ressenti ? `<div class="ressenti-rappel">Ton ressenti : « ${a.ressenti} »</div>` : ''}
      <button class="btn-reressenti" onclick="ouvrirRessenti(${a.id})">Modifier le ressenti</button>
    </div>
  ` : `
    <div class="ressenti-saisie" id="ressenti-zone-${a.id}">
      <textarea class="ressenti-input" id="ressenti-${a.id}"
                placeholder="Comment tu t'es senti sur cette séance ? (jambes, souffle, chaleur, douleurs...)"
                rows="2"></textarea>
      <button class="btn-debrief" onclick="envoyerRessenti(${a.id})">
        Générer le débrief
      </button>
    </div>
  `}
</div>
```

---

## JS — fonctions à ajouter

```javascript
async function envoyerRessenti(activityId) {
  const zone  = document.getElementById(`ressenti-zone-${activityId}`);
  const input = document.getElementById(`ressenti-${activityId}`);
  const texte = (input.value || "").trim();
  if (!texte) { input.focus(); return; }

  // État "en cours"
  zone.innerHTML = '<div class="debrief-loading">Le coach analyse ta séance…</div>';

  try {
    const resp = await fetch("/api/ressenti", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ activity_id: activityId, ressenti: texte }),
    });
    const data = await resp.json();
    if (!data.ok) throw new Error(data.error || "Erreur");

    const src = data.intervals_method === "laps"  ? " (laps détectés)"
              : data.intervals_method === "auto"  ? " (intervalles auto)"
              : "";
    zone.innerHTML = `
      <div class="debrief-affiche">
        <div class="debrief-label">🎯 Débrief coach${src}</div>
        <div class="debrief-texte">${data.debrief.replace(/\n/g, '<br>')}</div>
        <div class="ressenti-rappel">Ton ressenti : « ${texte} »</div>
      </div>`;
  } catch (e) {
    zone.innerHTML =
      `<div class="debrief-erreur">Erreur : ${e.message}.
       <button onclick="ouvrirRessenti(${activityId})">Réessayer</button></div>`;
  }
}

// Rouvre la zone de saisie (pour modifier un ressenti existant)
function ouvrirRessenti(activityId) {
  const block = document.querySelector(`.coach-block[data-activity-id="${activityId}"]`);
  block.innerHTML = `
    <div class="ressenti-saisie" id="ressenti-zone-${activityId}">
      <textarea class="ressenti-input" id="ressenti-${activityId}"
                placeholder="Comment tu t'es senti sur cette séance ?"
                rows="2"></textarea>
      <button class="btn-debrief" onclick="envoyerRessenti(${activityId})">
        Générer le débrief
      </button>
    </div>`;
}
```

---

## CSS — style minimal (adapte à ton thème)

```css
.coach-block { margin-top: 10px; border-top: 1px solid #2a2a2a; padding-top: 10px; }
.ressenti-input {
  width: 100%; background: #1a1a1a; color: #eee; border: 1px solid #333;
  border-radius: 8px; padding: 8px; font-size: 14px; resize: vertical;
}
.btn-debrief, .btn-reressenti {
  margin-top: 6px; padding: 6px 14px; border: none; border-radius: 8px;
  background: #fc5200; color: #fff; cursor: pointer; font-size: 13px;
}
.btn-reressenti { background: #333; }
.debrief-loading { color: #fc5200; font-style: italic; padding: 8px 0; }
.debrief-label { font-weight: 600; color: #fc5200; margin-bottom: 4px; }
.debrief-texte { color: #ddd; font-size: 14px; line-height: 1.5; }
.ressenti-rappel { color: #888; font-size: 12px; font-style: italic; margin-top: 6px; }
.debrief-erreur { color: #e74c3c; font-size: 13px; }
```
