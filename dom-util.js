// Various DOM-related JS helper functions, client-side

var escape_HTML = (function() {
    var text = txt('');
    var div = elt('div');
    div.appendChild(text);
    return function(s) {
        text.data = s;
        return div.innerHTML;
    };
})();

function elt_(d, n) {
    return d.createElement(n);
}

function txt_(d, s) {
    return d.createTextNode(s);
}

function elt(n) {
    return elt_(document, n);
}

function txt(s) {
    return txt_(document, s);
}

function elt_class(e, c) {
    var s = elt(e);
    s.className = c;
    return s;
}

function elt_txt_class(e, t, c) {
    var s = elt(e);
    s.className = c;
    s.appendChild(txt(t));
    return s;
}

function a_txt_class_url(t, c, u) {
    var a = elt_txt_class("a", t, c);
    a.setAttribute("href", u);
    return a;
}

function json_as_html_tree(j) {
    return json_as_html_tree_(document, j);
}

function json_as_html_tree_(d, j) {
    if (typeof(j) == 'object') {
        if (j == null) {
            return txt_(d, "null");
        } else {
            var table = elt_(d, 'table');
            table.className = "json-msg";
            var sk = sorted_keys(j);
            for (var i in sk) {
                var k = sk[i];
                var row = elt_(d, 'tr');
                var ke = elt_(d, 'td');
                var ve = elt_(d, 'td');
                row.className = "json-pair";
                ke.className = "json-key";
                ve.className = "json-val";
                ke.appendChild(txt_(d, k));
                ve.appendChild(json_as_html_tree_(d,j[k]));
                row.appendChild(ke);
                row.appendChild(ve);
                table.appendChild(row);
            }
            return table;
        }
    } else {
        return txt_(d, String(j));
    }
}

function prepend_child(parent, node) {
    var c = parent.firstChild;
    if (c) {
        parent.insertBefore(node, c);
    } else {
        parent.appendChild(node);
    }
}

function prepend_child_and_expire(parent, node, max_children) {
    prepend_child(parent, node);
    if (parent.childNodes.length > max_children) {
        parent.removeChild(parent.lastChild);
    }
}
