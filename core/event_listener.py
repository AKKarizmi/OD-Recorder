from playwright.sync_api import Page


EVENT_LISTENER_SCRIPT = r"""
(() => {
    if (window.__recorderEventListenersInstalled) return;
    window.__recorderEventListenersInstalled = true;

    function getXPath(element) {
        if (!element || !element.parentNode) return '';
        if (element.id && !generatedId(element.id)) return '//*[@id="' + element.id.replace(/"/g, '&quot;') + '"]';
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

    function cleanString(value) {
        return typeof value === 'string' && value.trim() ? value.trim() : null;
    }

    function generatedId(value) {
        return !value || /^(?:null|undefined|listbox-null)(?:-\d+)?$/i.test(value) || /^\d+$/.test(value);
    }

    function labelFor(element) {
        if (!element) return null;
        const labelledBy = cleanString(element.getAttribute('aria-labelledby'));
        if (labelledBy) {
            const text = labelledBy.split(/\s+/).map(id => document.getElementById(id)?.innerText || '').join(' ').trim();
            if (text) return text;
        }
        const aria = cleanString(element.getAttribute('aria-label'));
        if (aria) return aria;
        if (element.id && !generatedId(element.id)) {
            const explicit = document.querySelector('label[for="' + CSS.escape(element.id) + '"]');
            const text = cleanString(explicit?.innerText);
            if (text) return text;
        }
        if (element.labels?.length) {
            const text = cleanString(element.labels[0].innerText);
            if (text) return text;
        }
        const legend = element.closest('fieldset')?.querySelector('legend');
        if (cleanString(legend?.innerText)) return cleanString(legend.innerText);
        for (const wrapper of [
            element.closest('.form-group, .form-field, .field, [role="group"]'),
            element.closest('.multiselect, [data-multiselect]')?.parentElement,
            element.parentElement?.parentElement
        ]) {
            const label = wrapper?.querySelector?.('label');
            const heading = wrapper?.querySelector?.('h1, h2, h3, h4, h5, h6');
            if (cleanString(label?.innerText)) return cleanString(label.innerText);
            if (cleanString(heading?.innerText)) return cleanString(heading.innerText);
        }
        if (element.tagName === 'BUTTON' || element.tagName === 'A') {
            const actionText = cleanString(element.innerText);
            if (actionText) return actionText;
        }
        if (element.tagName === 'INPUT' && /^(submit|button|reset)$/i.test(element.type)) {
            const actionValue = cleanString(element.value);
            if (actionValue) return actionValue;
        }
        return cleanString(element.getAttribute('placeholder')) ||
            cleanString(element.getAttribute('name')) || cleanString(element.id);
    }

    function formInfo(element) {
        const form = element?.form || element?.closest?.('form');
        if (!form) return null;
        return {
            id: cleanString(form.getAttribute('id')),
            name: cleanString(form.getAttribute('name')),
            action: cleanString(form.getAttribute('action')),
            xpath: getXPath(form)
        };
    }

    function describeControl(element) {
        if (!element) return null;
        const widget = widgetIdentity(element);
        return {
            tag: element.tagName,
            inputType: cleanString(element.type),
            id: generatedId(element.id) ? null : cleanString(element.id),
            name: cleanString(element.getAttribute('name')),
            label: labelFor(element),
            xpath: getXPath(element),
            form: formInfo(element),
            widget_kind: widget ? 'multiselect' : null,
            widget_key: widget?.key || null,
            widget_label: widget?.label || null
        };
    }

    function widgetIdentity(element) {
        const root = element?.closest?.('.multiselect, [data-multiselect]');
        if (!root) return null;
        const controller = root.querySelector(
            'input.multiselect__input, input[role="combobox"], [role="combobox"], input[aria-controls]'
        );
        if (!controller) return null;
        const form = formInfo(controller);
        const scope = controller.form || document;
        const roots = Array.from(scope.querySelectorAll('.multiselect, [data-multiselect]'));
        const index = Math.max(0, roots.indexOf(root));
        const label = labelFor(controller);
        const stable = controller.id && !generatedId(controller.id) ? 'id:' + controller.id :
            (controller.name ? 'name:' + controller.name : (label ? 'label:' + label : 'index:' + index));
        const formKey = form?.id || form?.name || form?.action || 'document';
        return { key: formKey + '|multiselect|' + stable + '|' + index, label, root, controller, index };
    }

    function primitiveOptionValue(option, widget) {
        const nodes = [
            option,
            option.closest?.('.multiselect__element, li[data-value], li[data-id], li[data-key]')
        ].filter((node, index, all) => node && all.indexOf(node) === index);
        for (const node of nodes) {
            const attributes = [
                ['value', node.getAttribute('value')],
                ['data-value', node.getAttribute('data-value')],
                ['data-id', node.getAttribute('data-id')],
                ['data-key', node.getAttribute('data-key')],
                ['aria-valuetext', node.getAttribute('aria-valuetext')]
            ];
            for (const [source, value] of attributes) {
                if (value !== null && value !== '') return { value, source };
            }
        }
        const vueOption = option.__vue__?.option || option.parentElement?.__vue__?.option;
        if (vueOption && typeof vueOption === 'object') {
            for (const key of ['value', 'id', 'key', 'code']) {
                if (['string', 'number', 'boolean'].includes(typeof vueOption[key])) {
                    return { value: vueOption[key], source: 'component_state.' + key };
                }
            }
        }
        const hidden = widget?.root?.querySelector('input[type="hidden"]');
        if (hidden?.value && option.getAttribute('aria-selected') === 'true') {
            return { value: hidden.value, source: 'hidden_model_input' };
        }
        return { value: null, source: 'unavailable' };
    }

    function optionDetails(target) {
        const option = target?.closest?.(
            '[role="option"], .multiselect__option, .multiselect__element, li[aria-selected]'
        );
        if (!option) return null;
        const structuralRow = option.closest(
            '[role="option"], .multiselect__element, li[aria-selected], li[data-value], li[data-id]'
        );
        if (!structuralRow || option.closest('[role="status"], [aria-live]')) return null;
        const classText = [option.className, structuralRow.className].filter(value => typeof value === 'string').join(' ');
        if (/(?:^|[-_\s])(no[-_]?results?|no[-_]?options?|empty[-_]?message|loading)(?:$|[-_\s])/i.test(classText)) return null;
        if (structuralRow.getAttribute('aria-disabled') === 'true' &&
            !structuralRow.closest('.multiselect__element') &&
            !['value', 'data-value', 'data-id', 'data-key'].some(attribute => structuralRow.hasAttribute(attribute))) {
            return null;
        }
        const widget = widgetIdentity(option);
        if (!widget) return null;
        const canonical = option.matches('.multiselect__element')
            ? (option.querySelector('[role="option"], .multiselect__option') || option)
            : option;
        const label = cleanString(canonical.innerText || canonical.textContent || option.innerText || option.textContent);
        if (!label) return null;
        const machine = primitiveOptionValue(option, widget);
        if (machine.value === null && canonical !== option) {
            const nestedMachine = primitiveOptionValue(canonical, widget);
            if (nestedMachine.value !== null) Object.assign(machine, nestedMachine);
        }
        return {
            element: option,
            label,
            value: machine.value,
            value_source: machine.source,
            widget_key: widget.key,
            widget_label: widget.label,
            controller: describeControl(widget.controller),
            scoped_selector: {
                widget_key: widget.key, option_label: label,
                option_value: machine.value, option_value_source: machine.source
            }
        };
    }

    function semanticState(element, widgetRoot) {
        const root = widgetRoot || element?.closest?.('.multiselect, [data-multiselect]');
        if (root) {
            const controller = root.querySelector('input.multiselect__input, input[role="combobox"], [role="combobox"]');
            const widget = widgetIdentity(controller);
            const selectedMap = new Map();
            Array.from(root.querySelectorAll('.multiselect__single, .multiselect__tag, [role="option"][aria-selected="true"]'))
                .forEach(node => {
                    const label = cleanString(node.innerText || node.textContent);
                    if (!label) return;
                    const machine = primitiveOptionValue(node, widget);
                    const key = machine.value !== null ? 'v:' + String(machine.value) : 'l:' + label.toLocaleLowerCase();
                    selectedMap.set(key, { label, value: machine.value, value_source: machine.source });
                });
            const selected = Array.from(selectedMap.values());
            return {
                kind: 'multiselect', selected_values: selected,
                semantic_value: selected.map(item => item.label),
                value_present: selected.length > 0,
                expanded: controller?.getAttribute('aria-expanded') === 'true',
                focused: document.activeElement === controller,
                controller_value_present: !!controller?.value
            };
        }
        if (!element) return null;
        if (/^(checkbox|radio)$/i.test(element.type)) return { checked: !!element.checked };
        if (element.tagName === 'SELECT') {
            const selected = Array.from(element.selectedOptions || []).map(option => ({
                label: cleanString(option.label || option.text), value: option.value
            }));
            return { kind: 'native_select', selected_values: selected, value_present: selected.length > 0 };
        }
        const sensitive = /password|hidden|secret|token/i.test([
            element.type, element.name, element.id, element.autocomplete
        ].filter(Boolean).join(' '));
        const state = {
            value_present: typeof element.value === 'string' && element.value.length > 0,
            value_fingerprint: sensitive ? null : valueFingerprint(element),
            sensitive: sensitive
        };
        if (!sensitive && typeof element.value === 'string' && element.value) state.value = element.value;
        return state;
    }

    function semanticPageState() {
        const fields = Array.from(document.querySelectorAll('input, textarea, select')).map(element => ({
            xpath: getXPath(element),
            value_present: typeof element.value === 'string' && element.value.length > 0,
            value_fingerprint: valueFingerprint(element),
            value: safeValue(element),
            checked: /^(checkbox|radio)$/i.test(element.type) ? !!element.checked : null,
            selected_values: element.tagName === 'SELECT' ? Array.from(element.selectedOptions || []).map(option => ({
                label: cleanString(option.label || option.text), value: option.value
            })) : null
        }));
        const widgets = Array.from(document.querySelectorAll('.multiselect, [data-multiselect]')).map(root => {
            const controller = root.querySelector('input.multiselect__input, input[role="combobox"], [role="combobox"]');
            const identity = widgetIdentity(controller);
            return { widget_key: identity?.key || null, controller_xpath: getXPath(controller), ...semanticState(controller, root) };
        });
        return { url: location.href, fields, widgets };
    }

    function valueFingerprint(element) {
        if (!element || typeof element.value !== 'string' || !element.value) return null;
        const descriptor = [element.type, element.getAttribute('name'), element.id, element.autocomplete]
            .filter(Boolean).join(' ').toLowerCase();
        if (/password|hidden|secret|token|api.?key|authorization|cc-csc/.test(descriptor)) return null;
        let hash = 2166136261;
        for (let index = 0; index < element.value.length; index++) {
            hash ^= element.value.charCodeAt(index);
            hash = Math.imul(hash, 16777619);
        }
        return 'fnv1a32:' + (hash >>> 0).toString(16).padStart(8, '0') + ':' + element.value.length;
    }

    function safeValue(element) {
        if (!element || typeof element.value !== 'string' || !element.value) return null;
        const descriptor = [element.type, element.name, element.id, element.autocomplete,
            element.getAttribute('aria-label'), element.getAttribute('placeholder')]
            .filter(Boolean).join(' ').toLowerCase();
        if (/password|hidden|secret|token|csrf|api.?key|authorization|access[_ -]?token|refresh[_ -]?token|session[_ -]?id|cc-number|cc-csc|cc-exp/.test(descriptor)) return null;
        return element.value;
    }

    function effectiveTarget(original) {
        const label = original.closest?.('label');
        const labelledControl = label?.control || (label?.htmlFor ? document.getElementById(label.htmlFor) : null);
        if (labelledControl?.matches('input[type="checkbox"], input[type="radio"]')) return labelledControl;
        return original.closest?.('input, textarea, select, button, a, [role="option"], .multiselect__option, .multiselect__element') || original;
    }

    function widgetFor(option) {
        const direct = widgetIdentity(option);
        if (direct) {
            return {
                key: direct.key, index: direct.index, label: direct.label,
                controller: describeControl(direct.controller), root_xpath: getXPath(direct.root)
            };
        }
        const listbox = option.closest('[role="listbox"], .multiselect__content-wrapper, .multiselect__content');
        const listboxId = cleanString(listbox?.getAttribute('id'));
        let root = option.closest('.multiselect, [data-multiselect]');
        let controller = root?.querySelector('input.multiselect__input, input[role="combobox"], [role="combobox"], input[aria-controls]');
        const active = document.activeElement;
        if (!controller && active?.matches('input.multiselect__input, input[role="combobox"], [role="combobox"], input[aria-controls]')) {
            controller = active;
            root = active.closest('.multiselect, [data-multiselect]') || active;
        }
        if (!controller && listboxId && !generatedId(listboxId)) {
            const candidates = Array.from(document.querySelectorAll('[aria-controls]'))
                .filter(element => element.getAttribute('aria-controls') === listboxId);
            if (candidates.length === 1) {
                controller = candidates[0];
                root = controller.closest('.multiselect, [data-multiselect]') || controller;
            }
        }
        const widgetScope = controller?.form || document;
        const allRoots = Array.from(widgetScope.querySelectorAll('.multiselect, [data-multiselect]'));
        const index = Math.max(0, allRoots.indexOf(root));
        const controllerInfo = describeControl(controller);
        const stable = controllerInfo?.id ? 'id:' + controllerInfo.id :
            (controllerInfo?.name ? 'name:' + controllerInfo.name :
                (controllerInfo?.label ? 'label:' + controllerInfo.label : 'index:' + index));
        const form = controllerInfo?.form;
        const formKey = form?.id || form?.name || form?.action || 'document';
        return {
            key: formKey + '|multiselect|' + stable + '|' + index,
            index: index,
            label: controllerInfo?.label || null,
            controller: controllerInfo,
            root_xpath: getXPath(root),
        };
    }

    document.addEventListener('click', function(e) {
        const original = e.target;
        const target = effectiveTarget(original);
        if (!target || !target.tagName) return;
        const event = {
            type: 'click',
            tag: target.tagName,
            id: target.id,
            name: target.name,
            className: target.className,
            xpath: getXPath(target),
            timestamp: Date.now(),
            field: describeControl(target)
        };
        const option = optionDetails(original) || optionDetails(target);
        if (option) {
            event.selected_option = {
                label: option.label, value: option.value, value_source: option.value_source,
                widget_key: option.widget_key, widget_label: option.widget_label,
                controller: option.controller, scoped_selector: option.scoped_selector
            };
        }
        // Framework handlers run before this document-bubble listener.  A
        // microtask then observes the post-action widget state before another
        // physical user event can begin.
        queueMicrotask(() => {
            event.immediate_post_state = semanticState(
                event.selected_option?.controller ? document.activeElement : target,
                option?.element?.closest('.multiselect, [data-multiselect]') || null
            );
            event.immediate_page_state = semanticPageState();
            window.recordEvent(event);
        });
    }, false);

    for (const rawType of ['pointerdown', 'keydown']) {
        document.addEventListener(rawType, function(e) {
            const target = effectiveTarget(e.target);
            if (!target?.tagName) return;
            const event = {
                type: rawType, timestamp: Date.now(), field: describeControl(target),
                tag: target.tagName, inputType: target.type || null,
                key: rawType === 'keydown' ? (e.key.length > 1 ? e.key : 'Printable') : null,
                raw_only: true
            };
            const option = optionDetails(e.target) || optionDetails(target);
            if (option) event.selected_option = {
                label: option.label, value: option.value, value_source: option.value_source,
                widget_key: option.widget_key, widget_label: option.widget_label,
                controller: option.controller, scoped_selector: option.scoped_selector
            };
            window.recordEvent(event);
        }, true);
    }

    document.addEventListener('focusin', function(e) {
        const target = effectiveTarget(e.target);
        if (target?.matches('input, textarea, select')) {
            window.recordEvent({ type: 'focus', timestamp: Date.now(), field: describeControl(target),
                tag: target.tagName, id: target.id, name: target.getAttribute('name'), xpath: getXPath(target), inputType: target.type || null });
        }
    }, true);

    document.addEventListener('focusout', function(e) {
        const target = effectiveTarget(e.target);
        if (target?.matches('input, textarea, select')) {
            window.recordEvent({ type: 'blur', timestamp: Date.now(), field: describeControl(target),
                tag: target.tagName, id: target.id, name: target.getAttribute('name'), xpath: getXPath(target),
                inputType: target.type || null, value: safeValue(target), value_fingerprint: valueFingerprint(target),
                immediate_post_state: semanticState(target), immediate_page_state: semanticPageState() });
        }
    }, true);

    document.addEventListener('input', function(e) {
        const target = e.target;
        if (target && target.matches('input, textarea, select')) {
            window.recordEvent({
                type: 'input', tag: target.tagName, id: target.id, name: target.name,
                inputType: target.type || null, xpath: getXPath(target), timestamp: Date.now(),
                field: describeControl(target), value: safeValue(target), immediate_post_state: semanticState(target)
            });
        }
    }, true);

    document.addEventListener('change', function(e) {
        const target = e.target;
        if (target && target.matches('input, textarea, select')) {
            const event = {
                type: 'change', tag: target.tagName, id: target.id, name: target.name,
                inputType: target.type || null, xpath: getXPath(target), timestamp: Date.now(),
                field: describeControl(target), value: safeValue(target), value_fingerprint: valueFingerprint(target),
                immediate_post_state: semanticState(target), immediate_page_state: semanticPageState()
            };
            if (/^(checkbox|radio)$/i.test(target.type)) event.checked = !!target.checked;
            if (target.tagName === 'SELECT') {
                const selected = target.options[target.selectedIndex];
                event.selected_option = selected ? {
                    label: (selected.label || selected.text || '').trim(), value: selected.value,
                    controller: describeControl(target), widget_key: null,
                    scoped_selector: { field: describeControl(target), option_value: selected.value }
                } : null;
            }
            window.recordEvent(event);
        }
    }, true);

    document.addEventListener('submit', function(e) {
        const form = e.target;
        window.recordEvent({
            type: 'submit', tag: 'form', id: form.id,
            action: form.getAttribute('action'), method: form.method, timestamp: Date.now(),
            form: formInfo(form)
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
