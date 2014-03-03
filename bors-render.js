
function render_queue(queue) {
    var q = document.getElementById("queue");
    var hdr = elt_txt_class("td", "Bors queue: " + queue.length, "header");
    hdr.setAttribute("colspan", "5");
    var row = elt("tr");
    row.appendChild(hdr);
    q.appendChild(row);

    hdr = elt_txt_class("td", "updated " + updated.toISOString(), "header");
    hdr.setAttribute("colspan", "5");
    row = elt("tr");
    row.appendChild(hdr);
    q.appendChild(row);

    for (var i = queue.length - 1; i >= 0; --i) {
        var e = queue[i];
        row = elt("tr");

        var num = e["num"].toString();
        var num_cell = elt("td");
        var repo = e["src_owner"] + "/" + e["src_repo"] + "/"
            num_cell.appendChild(a_txt_class_url(num, "pull", "https://github.com/" + repo + "pull/" + num));
        row.appendChild(num_cell);

        var state = e["state"];
        row.appendChild(elt_txt_class("td", state, e["state"]));

        row.appendChild(elt_txt_class("td", e["prio"].toString(), "priority"));

        var ref = repo + e["ref"];
        row.appendChild(elt_txt_class("td", ref, "ref"));

        var t = e["title"]

            if (e["num_comments"] == 0 || 
                    e["last_comment"][2].indexOf("r+") == 0) {
                row.appendChild(elt_txt_class("td", t, "details"));
            } else {
                var last = e["last_comment"];
                var when = last[0]; 
                var who = last[1]; 
                var what = last[2]; 

                var td = elt_class("td", "details");
                td.appendChild(elt_txt_class("p", t, "title"));

                var c = elt_class("div", "comment");
                c.appendChild(elt_txt_class("div",
                            who + " " + when + " #" + e["num_comments"], 
                            "commentheader"));
                c.appendChild(elt_txt_class("div", what, "commentbody"));
                td.appendChild(c);

                row.appendChild(td);
            }


        q.appendChild(row);
    }
}

var qs = (function(a) {
    if (a == "") return {};
    var b = {};
    for (var i = 0; i < a.length; ++i) {
        var p=a[i].split('=');
        if (p.length != 2) continue;
        b[p[0]] = decodeURIComponent(p[1].replace(/\+/g, " "));
    }
    return b;
})(window.location.search.substr(1).split('&'));

function render_status() {
    var k = Object.keys(bors).sort();
    console.log(qs);
    if (k.length > 1) {
        console.log(qs["repo"], bors[qs["repo"]]);
        if (qs["repo"] && bors[qs["repo"]]) {
            render_queue(bors[qs["repo"]]);
        } else {
            console.log("feh");
            // render a list of repos
            var q = document.getElementById("queue");
            var hdr0 = elt_txt_class("td", "Repo", "header");
            var hdr1 = elt_txt_class("td", "PRs", "header");
            var row = elt("tr");
            row.appendChild(hdr0);
            row.appendChild(hdr1);
            q.appendChild(row);
            for (var i = 0; i < k.length; i++) {
                // TODO
                row = elt_class("tr", "summary");

                var name = k[i];
                var name_cell = elt("td");
                name_cell.appendChild(a_txt_class_url(name, "pull", "?repo="+name));
                row.appendChild(name_cell);

                var count = bors[k[i]].length
                    row.appendChild(elt_txt_class("td", count, ""));

                q.appendChild(row);
            }
        }
    } else {
        // render the first one
        render_queue(bors[keys[0]]);
    }
}


window.onload = render_status;
