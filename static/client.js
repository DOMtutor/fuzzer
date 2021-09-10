const warn = function (message) {
    $('.alerts').html($('.alerts').html() +
        "<div class=\"alert alert-success\"><a href=\"#\" class=\"close\" data-dismiss=\"alert\" aria-label=\"close\">&times;</a><strong>" + message + "</div>");
};

const display = function (cases, prefix) {
    // build tabs
    const keys = Object.keys(cases);

    for (i = 0; i < keys.length; i++) {
        const name = prefix + "" + (i + 1);
        const tab = $("<li><a data-toggle=\"tab\" href=\"#" + name + "\">" + name + "</a></li>");
        const content = $("<div id=\"" + name + "\" class=\"tab-pane fade\">");

        if (i === 0) {
            tab.addClass("active");
            content.addClass("in");
            content.addClass("active");
        }

        const text = $("<textarea rows=10, cols=80 readonly=\"true\" wrap=\"off\"></textarea>");
        text.val(JSON.stringify(cases[keys[i]], null, 2))

        const copycase = $("<button class=\"btn btn-md btn-primary\" data-clipboard-action=\"copy\">Case</button>");
        new Clipboard(copycase.get()[0]);
        const copyanswer = $("<button class=\"btn btn-md btn-primary\" data-clipboard-action=\"copy\">Answer</button>");
        new Clipboard(copyanswer.get()[0]);
        const copytext = $("<button class=\"btn btn-md btn-primary\" data-clipboard-action=\"copy\">Text</button>");
        new Clipboard(copytext.get()[0]);

        // Find files
        const files = Object.keys(cases[keys[i]]);
        let infile = "";
        let solution = "";
        for (let j = 0; j < files.length; j++) {
            if (files[j].endsWith(".in")) {
                infile = cases[keys[i]][files[j]]
            } else if (files[j].endsWith(".ans")) {
                solution = cases[keys[i]][files[j]];
            }
        }

        copycase.attr("data-clipboard-text", infile);
        copyanswer.attr("data-clipboard-text", solution);
        copytext.attr("data-clipboard-text",
            "Here's a case to think about:\n" + infile + "\nThe correct answer should be:\n" + solution
        );

        $("#casetabs").append(tab);
        content.append(text);
        content.append($("<br />"));
        content.append(copycase);
        content.append(copyanswer);
        content.append(copytext);
        $("#casetabcontents").append(content);
    }
};

// File name listener
const change_listener = function (event) {
    const source = $("#source").val();

    const java_regex = /public\s+(static\s+)?(final\s+)?class\s+([a-zA-Z_$][a-zA-Z\d_$]*)/;
    const java_match = source.match(java_regex);
    if (java_match && java_match[3]) {
        console.log("Guessing source to be java, class name " + java_match[3]);
        $("#source_lang").val("java").change();
        $("#java_name").val(java_match[3]);
        return;
    }

    const python_regex = [/sys\.stdin/, /\sprint\(/, /for\s+\S+\s+in\s+/];
    for (let regex of python_regex) {
        const pythonname = source.match(regex);
        if (pythonname) {
            console.log("Guessing source to be python, since it matches " + regex.toString());
            $("#source_lang").val("python").change();
            return;
        }
    }

    console.log("Guessing source be c++")
    $("#source_lang").val("cpp").change();
};

const submit_listener = function (event) {
    event.preventDefault();

    // Language
    const source_lang = $('#source_lang').val();
    let lang;
    let source_name;
    if (source_lang === "python") {
        lang = "python";
        source_name = "main.py";
    } else if (source_lang === "java") {
        const classname = $("#java_name").val();
        if (!classname) {
            warn("No Java class name!");
            return;
        }

        lang = "java";
        source_name = classname + ".java";
    } else if (source_lang === "cpp") {
        lang = "cpp";
        source_name = "a.cpp";
    } else {
        warn("Source language unknown!");
        return;
    }

    // Problem
    const problem = $('#problem_name').val();
    if (!problem) {
        warn("No problem selected!");
        return;
    }

    // Secret file
    let secret_name = $('#secret_name').val();
    if (secret_name === "") {
        secret_name = "small1";
    }

    const time_limit = parseInt($('#time_limit').val());

    // Button state
    const l = Ladda.create(document.querySelector('#submitter'));
    l.start();

    console.log("Submitting as " + source_name);
    let source = {};
    source[source_name] = $('#source').val();

    const request = {
        "problem": problem,
        "lang": lang,
        "sources": source,
        "secret_file": secret_name,
        "time_limit": time_limit
    };

    console.log("Starting fuzzing " + JSON.stringify(request, null, 2));

    let uuid = 0;

    // Periodical update function - uses the submission uuid
    const update_fun = function () {
        console.log("Sending fuzzing update query for " + uuid);
        $.ajax({
            type: 'GET',
            url: "/submission/" + uuid,
            success: function (response) {
                console.log("Got fuzzing update" + JSON.stringify(response, null, 2));

                if (response.success) {
                    if (!response.state.finished) {
                        $("#log").val(response.state.log + "\n\nStill running...");
                        // continue updating
                        setTimeout(update_fun, 500);
                    } else {
                        $("#log").val(response.state.log);
                        // console.log(JSON.stringify(state.cases, null, 2))

                        if ("cases" in response.state) {
                            display(response.state.cases.wa, "WA");
                            display(response.state.cases.rte, "RTE");
                        }

                        l.stop();
                    }
                    // Scroll the textarea all the way down
                    $("#log").scrollTop($("#log")[0].scrollHeight);
                } else {
                    console.log("Unsuccessful update poll " + JSON.stringify(response, null, 2));
                    warn("Error in update request");
                    l.stop();
                }
            },
            error: function (response) {
                warn("Error in update request.");
                l.stop();
            }
        });
    };

    // Empty alerts
    $('.alerts').html("");

    // Empty result form
    $("#log").val("");
    $("#cases").val("");

    $("#casetabs").empty();
    $("#casetabcontents").empty();

    // Initial submission request - if succeeded, will start periodical update requests via updatefun
    $.ajax({
        type: 'POST',
        url: "/submission",
        contentType: 'application/json',
        data: JSON.stringify(request),
        success: function (response) {
            if (response.success) {
                $("#uuid").val(response.id);
                uuid = response.id;
                // updating every x seconds while still running
                console.log("Started fuzzing with id " + response.id);
                // Start update polling
                update_fun();
            } else {
                warn("Could not start fuzzing " + response.errors);
                console.log("Could not start fuzzing " + JSON.stringify(response, null, 2))
                // reenable button
                l.stop();
            }
        },
        error: function (response) {
            warn("Error in fuzzing request.");
            l.stop();
        }
    });
};

$("#source").blur(change_listener);
$("#source").change(change_listener);

$("#target").submit(submit_listener);

$("#source_lang").change(function (event) {
    if ($("#source_lang").val() === "java") {
        $("#java_name").prop("disabled", false);
    } else {
        $("#java_name").prop("disabled", true);
        $("#java_name").val("");
    }
});

// Populate typeahead
$.get('/problems', function (data) {
    console.log(data);
    $("#problem_name").typeahead({source: data.problems});
    $("#problem_name").prop("disabled", false);
}, 'json');
