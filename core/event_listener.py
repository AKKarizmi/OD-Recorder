from playwright.sync_api import Page


EVENT_LISTENER_SCRIPT = r"""
(() => {
    if (window.__recorderEventListenersInstalled) return;
    window.__recorderEventListenersInstalled = true;

    function getXPath(element) {
        if (!element || !element.parentNode) return '';
        if (element.id) return '//*[@id="' + element.id.replace(/"/g, '&quot;') + '"]';
        if (element === document.body) return '/html/body';
        let ix = 0;
        const siblings = element.parentNode.childNodes;
        for (let i = 0; i < siblings.length; i++) {
            const sibling = siblings[i];
            if (sibling === element) {
                return getXPath(element.parentNode) + '/' + element.tagName.toLowerCase() + '[' + (ix + 1) + ']';
            }
            if (sibling.nodeType === 1 && sibling.tagName === element.tagName) ix++;
        }
        return '';
    }

    document.addEventListener('click', function(e) {
        const target = e.target;
        if (!target || !target.tagName) return;
        window.recordEvent({
            type: 'click',
            tag: target.tagName,
            id: target.id,
            name: target.name,
            className: target.className,
            xpath: getXPath(target),
            text: target.innerText?.trim().slice(0, 100),
            value: target.value || null,
            timestamp: Date.now()
        });
    }, true);

    document.addEventListener('input', function(e) {
        const target = e.target;
        if (target && target.matches('input, textarea, select')) {
            window.recordEvent({
                type: 'input', tag: target.tagName, id: target.id, name: target.name,
                xpath: getXPath(target), value: target.value, timestamp: Date.now()
            });
        }
    }, true);

    document.addEventListener('change', function(e) {
        const target = e.target;
        if (target && target.matches('input, textarea, select')) {
            window.recordEvent({
                type: 'change', tag: target.tagName, id: target.id, name: target.name,
                xpath: getXPath(target), value: target.value, timestamp: Date.now()
            });
        }
    }, true);

    document.addEventListener('submit', function(e) {
        const form = e.target;
        window.recordEvent({
            type: 'submit', tag: 'form', id: form.id,
            action: form.action, method: form.method, timestamp: Date.now()
        });
    }, true);
})();
"""


def inject_event_listeners(page: Page, event_callback):
    """Install listeners for future documents and the currently loaded document."""
    page.expose_function("recordEvent", event_callback)
    page.add_init_script(EVENT_LISTENER_SCRIPT)
    # add_init_script only affects future navigations; recording can begin on
    # an already-loaded, operator-positioned page.
    try:
        for frame in page.frames:
            try:
                frame.evaluate(EVENT_LISTENER_SCRIPT)
            except Exception:
                continue
    except Exception:
        # The document may be between navigations. The init script will cover
        # the next document, and the recorder will continue normally.
        try:
            page.evaluate(EVENT_LISTENER_SCRIPT)
        except Exception:
            pass
