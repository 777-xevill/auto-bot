// Walks the DOM and returns the elements Claude can read or act on.
// Each reported element gets a stable data-ab-ref attribute (ref_N) that
// survives until the page navigates, so refs Claude holds stay valid.
(opts) => {
  const filter = opts.filter || null;          // null (visible) | "interactive" | "all"
  const maxDepth = opts.depth || 15;
  const prefix = opts.prefix || "ref_";
  const ATTR = "data-ab-ref";
  const W = window;
  if (!W.__abSeq) W.__abSeq = 0;

  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "HEAD", "META", "LINK", "svg", "SVG"]);
  const INTERACTIVE_ROLES = new Set([
    "button", "link", "textbox", "searchbox", "checkbox", "radio", "combobox", "listbox",
    "option", "menuitem", "menuitemcheckbox", "menuitemradio", "tab", "switch", "slider",
    "spinbutton", "treeitem", "gridcell"]);
  const LANDMARKS = { NAV: "navigation", MAIN: "main", FORM: "form", DIALOG: "dialog",
                      HEADER: "banner", FOOTER: "contentinfo", ASIDE: "complementary" };
  const vw = innerWidth, vh = innerHeight;
  const out = [];

  const clean = (s, n) => (s || "").replace(/\s+/g, " ").trim().slice(0, n);

  function findRef(ref) {
    const sel = `[${ATTR}="${ref}"]`;
    const direct = document.querySelector(sel);
    if (direct) return direct;
    const stack = [document.documentElement];
    while (stack.length) {
      const el = stack.pop();
      if (el.shadowRoot) {
        const hit = el.shadowRoot.querySelector(sel);
        if (hit) return hit;
        stack.push(...el.shadowRoot.children);
      }
      stack.push(...el.children);
    }
    return null;
  }

  function refFor(el) {
    let r = el.getAttribute(ATTR);
    if (!r) { r = prefix + (++W.__abSeq); el.setAttribute(ATTR, r); }
    return r;
  }

  function roleOf(el) {
    const explicit = el.getAttribute("role");
    if (explicit) return explicit.trim().split(/\s+/)[0];
    const tag = el.tagName;
    if (tag === "A") return el.hasAttribute("href") ? "link" : null;
    if (tag === "BUTTON" || tag === "SUMMARY") return "button";
    if (tag === "INPUT") {
      const t = (el.type || "text").toLowerCase();
      if (["button", "submit", "reset", "image", "file"].includes(t)) return "button";
      if (t === "checkbox") return "checkbox";
      if (t === "radio") return "radio";
      if (t === "range") return "slider";
      if (t === "number") return "spinbutton";
      if (t === "search") return "searchbox";
      if (t === "hidden") return null;
      return "textbox";
    }
    if (tag === "TEXTAREA") return "textbox";
    if (tag === "SELECT") return el.multiple ? "listbox" : "combobox";
    if (tag === "OPTION") return "option";
    if (tag === "IMG") return "img";
    if (/^H[1-6]$/.test(tag)) return "heading";
    if (LANDMARKS[tag]) return LANDMARKS[tag];
    if (el.isContentEditable && !(el.parentElement && el.parentElement.isContentEditable)) return "textbox";
    return null;
  }

  function isInteractive(el, role, style, parentCursor) {
    if (role && INTERACTIVE_ROLES.has(role)) return true;
    if (el.hasAttribute("onclick")) return true;
    const ti = el.getAttribute("tabindex");
    if (ti !== null && ti !== "-1" && !["main", "dialog", "navigation"].includes(role)) return true;
    return style.cursor === "pointer" && parentCursor !== "pointer";
  }

  function textOf(el) { return clean(el.innerText || el.textContent, 150); }

  function nameOf(el, role) {
    let n = el.getAttribute("aria-label");
    if (!n) {
      const ids = el.getAttribute("aria-labelledby");
      if (ids) n = ids.split(/\s+/).map(id => { const e = document.getElementById(id); return e ? textOf(e) : ""; }).join(" ");
    }
    if (!n && el.labels && el.labels.length) n = textOf(el.labels[0]);
    if (!n && (el.tagName === "INPUT" || el.tagName === "TEXTAREA")) {
      n = el.getAttribute("placeholder") ||
          (["button", "submit", "reset"].includes((el.type || "").toLowerCase()) ? el.value : "");
    }
    if (!n && el.tagName === "IMG") n = el.getAttribute("alt");
    if (!n && (role === "link" || role === "button")) {
      const img = el.querySelector("img[alt], [aria-label]");
      if (img) n = img.getAttribute("alt") || img.getAttribute("aria-label");
    }
    if (!n && !["textbox", "searchbox", "combobox", "listbox", "main", "navigation", "form",
                "dialog", "banner", "contentinfo", "complementary"].includes(role)) n = textOf(el);
    if (!n) n = el.getAttribute("title") || "";
    return clean(n, 150);
  }

  function directText(el) {
    let t = "";
    for (const c of el.childNodes) if (c.nodeType === 3) t += c.textContent;
    return clean(t, 200);
  }

  function extras(el, role, node) {
    const tag = el.tagName;
    if (role === "link") node.href = clean(el.getAttribute("href"), 120);
    if (tag === "INPUT" || tag === "TEXTAREA") {
      const t = (el.type || "").toLowerCase();
      if (t && !["text", "textarea", "submit", "button", "checkbox", "radio"].includes(t)) node.type = t;
      if (el.value && !["submit", "button", "checkbox", "radio"].includes(t))
        node.value = t === "password" ? "•••" : clean(el.value, 80);
    }
    if (tag === "SELECT" && el.selectedOptions && el.selectedOptions.length)
      node.value = clean(el.selectedOptions[0].text, 80);
    if (el.isContentEditable && role === "textbox") node.value = clean(el.innerText, 80);
    if (role === "checkbox" || role === "radio" || role === "switch") {
      node.checked = el.checked === true || el.getAttribute("aria-checked") === "true";
    }
    if (role === "heading") node.level = /^H[1-6]$/.test(tag) ? +tag[1] : +(el.getAttribute("aria-level") || 2);
    if (el.disabled || el.getAttribute("aria-disabled") === "true") node.disabled = true;
    const exp = el.getAttribute("aria-expanded");
    if (exp !== null) node.expanded = exp === "true";
  }

  function walk(el, depth, parentCursor, insideNamed) {
    if (out.length >= 5000 || SKIP.has(el.tagName)) return;
    const style = getComputedStyle(el);
    if (style.display === "none" || parseFloat(style.opacity) === 0) return;
    if (el.getAttribute("aria-hidden") === "true") return;
    const hidden = style.visibility === "hidden" || style.visibility === "collapse";
    const rect = el.getBoundingClientRect();
    const hasBox = rect.width > 0 && rect.height > 0;
    const inView = hasBox && rect.bottom > 0 && rect.right > 0 && rect.top < vh && rect.left < vw;

    let reported = false, named = insideNamed;
    if (!hidden && hasBox && (filter === "all" || inView)) {
      const role = roleOf(el);
      const interactive = isInteractive(el, role, style, parentCursor);
      let node = null;
      if (interactive) {
        node = { role: role || "clickable", name: nameOf(el, role) };
      } else if (filter !== "interactive") {
        if (role === "heading" || (role === "img" && nameOf(el, role))) node = { role, name: nameOf(el, role) };
        else if (!insideNamed) { const t = directText(el); if (t) node = { role: "text", name: t }; }
      }
      if (node) {
        node.ref = refFor(el);
        node.depth = depth;
        node.interactive = interactive;
        node.inView = inView;
        extras(el, node.role, node);
        out.push(node);
        reported = true;
        if (interactive || node.role === "heading") named = true;
      }
    }
    const next = reported ? depth + 1 : depth;
    if (next >= maxDepth) return;
    if (el.shadowRoot) for (const c of el.shadowRoot.children) walk(c, next, style.cursor, named);
    for (const c of el.children) walk(c, next, style.cursor, named);
  }

  let root = document.body || document.documentElement;
  if (opts.ref) {
    root = findRef(opts.ref);
    if (!root) return { error: `${opts.ref} is stale or not found on the current page. Re-read the page to get fresh references.` };
  }
  if (root) walk(root, 0, "auto", false);
  return { nodes: out, title: document.title, url: location.href,
           scrollY: Math.round(scrollY), scrollHeight: document.documentElement.scrollHeight, vh };
}
