
function render_queue() {
    var q = document.getElementById("queue");
    var hdr = elt_txt_class("td", "Bors queue: " + bors.length, "header");
    hdr.setAttribute("colspan", "5");
    var row = elt("tr");
	row.appendChild(hdr);
    q.appendChild(row);

	hdr = elt_txt_class("td", "updated " + updated.toISOString(), "header");
    hdr.setAttribute("colspan", "5");
	row = elt("tr");
	row.appendChild(hdr);
    q.appendChild(row);

    for (var i = bors.length - 1; i >= 0; --i) {
        var e = bors[i];
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


window.onload = render_queue;
