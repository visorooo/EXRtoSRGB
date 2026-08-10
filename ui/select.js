/*
 * Select — a custom dropdown matching the one in the VISOR invoice app.
 *
 * That app gets this from @radix-ui/react-select, which needs React. This is the
 * same component's behaviour and motion rebuilt in plain DOM so the two tools
 * feel like one family: trigger scales on press, chevron rotates, the panel
 * grows out of the trigger it opened from, items highlight from pointer AND
 * keyboard through one state, and the check mark pops in.
 *
 * It replaces a real <select> in place and keeps it in the DOM as the source of
 * truth, so `$('format').value` and `onchange` keep working exactly as before
 * and nothing else in app.js has to know this exists.
 *
 * Behaviour deliberately copied from Radix:
 *   - Up/Down/Home/End move the highlight without committing
 *   - Enter/Space commit, Escape cancels and restores
 *   - printable characters do type-ahead
 *   - the panel flips above the trigger when it would overflow the window
 *   - pointer and keyboard share one [data-highlighted] state so they cannot
 *     disagree about what is focused
 */

const CHEVRON =
  '<svg class="select-chevron" width="12" height="12" viewBox="0 0 12 12" ' +
  'fill="none" aria-hidden="true">' +
  '<path d="M3 4.5L6 7.5L9 4.5" stroke="currentColor" stroke-width="1.5" ' +
  'stroke-linecap="round" stroke-linejoin="round"/></svg>';

const CHECK =
  '<svg class="select-item__check" width="12" height="12" viewBox="0 0 12 12" ' +
  'fill="none" aria-hidden="true">' +
  '<path d="M2.5 6.5L4.5 8.5L9.5 3.5" stroke="currentColor" stroke-width="1.75" ' +
  'stroke-linecap="round" stroke-linejoin="round"/></svg>';

let openInstance = null;

class Select {
  constructor(native) {
    this.native = native;
    this.open = false;
    this.highlight = -1;
    this.typeahead = '';
    this.typeaheadTimer = null;

    native.classList.add('select-native');

    this.trigger = document.createElement('button');
    this.trigger.type = 'button';
    this.trigger.className = 'select-trigger';
    this.trigger.setAttribute('data-state', 'closed');
    this.trigger.setAttribute('aria-haspopup', 'listbox');
    this.trigger.setAttribute('aria-expanded', 'false');
    if (native.id) this.trigger.setAttribute('aria-labelledby', native.id + '-lbl');

    this.label = document.createElement('span');
    this.label.className = 'select-value';
    this.trigger.appendChild(this.label);
    this.trigger.insertAdjacentHTML('beforeend', CHEVRON);

    native.parentNode.insertBefore(this.trigger, native.nextSibling);

    this.panel = document.createElement('div');
    this.panel.className = 'select-panel';
    this.panel.setAttribute('role', 'listbox');
    this.viewport = document.createElement('div');
    this.viewport.className = 'select-viewport';
    this.panel.appendChild(this.viewport);

    this.trigger.addEventListener('click', () => this.toggle());
    this.trigger.addEventListener('keydown', (e) => this.onTriggerKey(e));

    // A <select> repopulated from Python must refresh the trigger label.
    this.observer = new MutationObserver(() => this.sync());
    this.observer.observe(native, { childList: true, subtree: true, attributes: true });
    native.addEventListener('change', () => this.sync());

    this.sync();
  }

  get options() {
    return Array.from(this.native.options);
  }

  sync() {
    const opt = this.native.selectedOptions[0];
    this.label.textContent = opt ? opt.textContent.trim() : '';
    this.trigger.disabled = this.native.disabled;
    this.trigger.classList.toggle('is-empty', !opt);
    if (this.open) this.renderItems();
  }

  /* -- open / close --------------------------------------------------- */

  toggle() {
    this.open ? this.close() : this.openPanel();
  }

  openPanel() {
    if (this.native.disabled || !this.options.length) return;
    if (openInstance && openInstance !== this) openInstance.close(true);
    openInstance = this;

    this.open = true;
    this.highlight = this.native.selectedIndex;
    this.trigger.setAttribute('data-state', 'open');
    this.trigger.setAttribute('aria-expanded', 'true');

    this.panel.setAttribute('data-state', 'open');
    document.body.appendChild(this.panel);
    this.renderItems();
    this.position();

    this.onDocDown = (e) => {
      if (!this.panel.contains(e.target) && !this.trigger.contains(e.target)) {
        this.close();
      }
    };
    this.onKey = (e) => this.onPanelKey(e);
    this.onScroll = () => this.position();
    document.addEventListener('mousedown', this.onDocDown, true);
    document.addEventListener('keydown', this.onKey, true);
    window.addEventListener('resize', this.onScroll);
    window.addEventListener('scroll', this.onScroll, true);

    this.scrollHighlightIntoView('auto');
  }

  close(immediate) {
    if (!this.open) return;
    this.open = false;
    if (openInstance === this) openInstance = null;

    this.trigger.setAttribute('data-state', 'closed');
    this.trigger.setAttribute('aria-expanded', 'false');

    document.removeEventListener('mousedown', this.onDocDown, true);
    document.removeEventListener('keydown', this.onKey, true);
    window.removeEventListener('resize', this.onScroll);
    window.removeEventListener('scroll', this.onScroll, true);

    const panel = this.panel;
    if (immediate) {
      panel.remove();
      return;
    }
    // Let the exit animation finish before removing, but never strand the node
    // if the animation is disabled by prefers-reduced-motion.
    panel.setAttribute('data-state', 'closed');
    const done = () => panel.remove();
    panel.addEventListener('animationend', done, { once: true });
    setTimeout(done, 220);
  }

  /* -- layout ---------------------------------------------------------- */

  position() {
    const r = this.trigger.getBoundingClientRect();
    const margin = 8;
    this.panel.style.minWidth = r.width + 'px';
    this.panel.style.left = Math.max(margin, r.left) + 'px';

    const belowRoom = window.innerHeight - r.bottom - margin;
    const aboveRoom = r.top - margin;
    // Flip above only when below genuinely cannot hold it and above is better.
    const flip = belowRoom < 180 && aboveRoom > belowRoom;

    const max = Math.max(120, (flip ? aboveRoom : belowRoom));
    this.panel.style.maxHeight = max + 'px';

    if (flip) {
      this.panel.style.top = '';
      this.panel.style.bottom = window.innerHeight - r.top + 4 + 'px';
      this.panel.style.transformOrigin = 'bottom center';
    } else {
      this.panel.style.bottom = '';
      this.panel.style.top = r.bottom + 4 + 'px';
      this.panel.style.transformOrigin = 'top center';
    }
  }

  renderItems() {
    this.viewport.innerHTML = '';
    this.options.forEach((opt, i) => {
      const item = document.createElement('div');
      item.className = 'select-item';
      item.setAttribute('role', 'option');
      // A disabled <option> is invisible in the native select, which is hidden -
      // so without this the panel offers a choice the value can never take.
      if (opt.disabled) {
        item.setAttribute('data-disabled', '');
        item.setAttribute('aria-disabled', 'true');
        if (opt.title) item.title = opt.title;
      }
      if (i === this.native.selectedIndex) {
        item.setAttribute('data-state', 'checked');
        item.setAttribute('aria-selected', 'true');
      }
      if (i === this.highlight) item.setAttribute('data-highlighted', '');

      const text = document.createElement('span');
      text.className = 'select-item__text';
      text.textContent = opt.textContent.trim();
      item.appendChild(text);

      if (i === this.native.selectedIndex) {
        item.insertAdjacentHTML('beforeend', CHECK);
      }

      if (!opt.disabled) {
        item.addEventListener('mouseenter', () => this.setHighlight(i, false));
        item.addEventListener('click', () => this.commit(i));
      }
      this.viewport.appendChild(item);
    });
  }

  setHighlight(i, scroll = true) {
    const items = this.viewport.children;
    if (this.highlight >= 0 && items[this.highlight]) {
      items[this.highlight].removeAttribute('data-highlighted');
    }
    this.highlight = i;
    if (items[i]) {
      items[i].setAttribute('data-highlighted', '');
      if (scroll) this.scrollHighlightIntoView();
    }
  }

  scrollHighlightIntoView(behavior = 'auto') {
    const el = this.viewport.children[this.highlight];
    if (el) el.scrollIntoView({ block: 'nearest', behavior });
  }

  /*
   * The next selectable index from `from`, walking in `dir`.
   *
   * Returns `from` itself when nothing is reachable, so a list that is entirely
   * disabled cannot move the highlight or spin forever.
   */
  nextEnabled(from, dir) {
    const opts = this.options;
    for (let i = from + dir; i >= 0 && i < opts.length; i += dir) {
      if (!opts[i].disabled) return i;
    }
    return this.highlight;
  }

  firstEnabled(dir) {
    const opts = this.options;
    const start = dir > 0 ? 0 : opts.length - 1;
    if (opts[start] && !opts[start].disabled) return start;
    return this.nextEnabled(start, dir);
  }

  commit(i) {
    if (i < 0 || i >= this.options.length) return;
    if (this.options[i].disabled) return;
    const changed = i !== this.native.selectedIndex;
    this.native.selectedIndex = i;
    this.sync();
    this.close();
    this.trigger.focus();
    if (changed) {
      this.native.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }

  /* -- keyboard -------------------------------------------------------- */

  onTriggerKey(e) {
    if (this.open) return;
    if (['ArrowDown', 'ArrowUp', 'Enter', ' '].includes(e.key)) {
      e.preventDefault();
      this.openPanel();
    }
  }

  onPanelKey(e) {
    if (!this.open) return;
    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault();
        this.setHighlight(this.nextEnabled(this.highlight, 1));
        break;
      case 'ArrowUp':
        e.preventDefault();
        this.setHighlight(this.nextEnabled(this.highlight, -1));
        break;
      case 'Home':
        e.preventDefault();
        this.setHighlight(this.firstEnabled(1));
        break;
      case 'End':
        e.preventDefault();
        this.setHighlight(this.firstEnabled(-1));
        break;
      case 'Enter':
      case ' ':
        e.preventDefault();
        this.commit(this.highlight);
        break;
      case 'Escape':
        e.preventDefault();
        e.stopPropagation();
        this.close();
        this.trigger.focus();
        break;
      case 'Tab':
        this.close(true);
        break;
      default:
        if (e.key.length === 1) this.doTypeahead(e.key);
    }
  }

  doTypeahead(ch) {
    clearTimeout(this.typeaheadTimer);
    this.typeahead += ch.toLowerCase();
    this.typeaheadTimer = setTimeout(() => (this.typeahead = ''), 600);
    const i = this.options.findIndex((o) =>
      !o.disabled &&
      o.textContent.trim().toLowerCase().startsWith(this.typeahead));
    if (i >= 0) this.setHighlight(i);
  }
}

/* Upgrade every <select> on the page, once. Exposed as a global rather than an
   ES export because the page is loaded over file://, where module imports are
   blocked by the opaque origin. */
window.upgradeSelects = function upgradeSelects(root = document) {
  root.querySelectorAll('select:not(.select-native)').forEach((el) => {
    el._select = new Select(el);
  });
};
