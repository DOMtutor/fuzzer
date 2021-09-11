const warn = function (message) {
    const alerts = $('.alerts');
    alerts.html(alerts.html() +
        "<div class=\"alert alert-success\"><a href=\"#\" class=\"close\" data-dismiss=\"alert\" aria-label=\"close\">&times;</a><strong>" + message + "</div>");
};

const display = function (cases) {
    const keys = Object.keys(cases);

    for (let i = 0; i < keys.length; i++) {
        const type = keys[i];
        const name = type + "" + (i + 1);
        const tab = $('<li><a data-toggle="tab" href="#' + name + '">' + name + '</a></li>');
        const content = $('<div id="' + name + '" class="tab-pane fade">');

        if (i === 0) {
            tab.addClass("active");
            content.addClass("in");
            content.addClass("active");
        }

        const text = $('<textarea rows=10, cols=80 readonly="readonly" wrap="soft"></textarea>');
        text.val(JSON.stringify(cases[keys[i]], null, 2))

        const copy_case = $('<button class="btn btn-md btn-primary" data-clipboard-action="copy">Case</button>');
        new Clipboard(copy_case.get()[0]);
        const copy_answer = $('<button class="btn btn-md btn-primary" data-clipboard-action="copy">Answer</button>');
        new Clipboard(copy_answer.get()[0]);
        const copy_text = $('<button class="btn btn-md btn-primary" data-clipboard-action="copy">Text</button>');
        new Clipboard(copy_text.get()[0]);

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

        copy_case.attr("data-clipboard-text", infile);
        copy_answer.attr("data-clipboard-text", solution);
        copy_text.attr("data-clipboard-text",
            "Here's a case to think about:\n" + infile + "\nThe correct answer should be:\n" + solution
        );

        $("#cases_tabs").append(tab);
        content.append(text);
        content.append($("<br />"));
        content.append(copy_case);
        content.append(copy_answer);
        content.append(copy_text);
        $("#cases_tab_contents").append(content);
    }
};

const source_change = function () {
    const source = $("#source").val();
    const source_lang = $("#source_lang");

    const java_regex = /public\s+(static\s+)?(final\s+)?class\s+([a-zA-Z_$][a-zA-Z\d_$]*)/;
    const java_match = source.match(java_regex);
    if (java_match && java_match[3]) {
        console.log("Guessing source to be java, class name " + java_match[3]);
        source_lang.val("java").change();
        $("#java_name").val(java_match[3]);
        return;
    }

    const python_regex = [/sys\.stdin/, /\sprint\(/, /for\s+\S+\s+in\s+/];
    for (let regex of python_regex) {
        if (source.match(regex)) {
            console.log("Guessing source to be python, since it matches " + regex.toString());
            source_lang.val("python").change();
            return;
        }
    }

    const cpp_regex = [/#include/, /scanf/];
    for (let regex of cpp_regex) {
        if (source.match(regex)) {
            console.log("Guessing source to be cpp, since it matches " + regex.toString());
            source_lang.val("cpp").change();
            return;
        }
    }

    console.log("No source guess")
    source_lang.val("unknown").change();
}

const toggle_button = function (ready) {
    if (ready) {
        $('#submit-spinner').removeClass("spinner-border spinner-border-sm");
    } else {
        $('#submit-spinner').addClass("spinner-border spinner-border-sm");
    }
    $('#submit').prop("disabled", !ready);
}

const submit_problem = function () {
    // Problem
    const problem = $('#problem_name').val();
    if (!problem) {
        warn("No problem selected!");
        return;
    }

    // Secret file
    let secret_name = $('#secret_name').val();

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
        lang = null;
    }

    const time_limit = parseInt($('#time_limit').val());
    const runs = parseInt($('#runs').val());

    console.log("Submitting as " + source_name);
    let source = {};
    source[source_name] = $('#source').val();

    const request = {
        "problem": problem,
        "lang": lang,
        "sources": source,
        "secret_file": secret_name,
        "time_limit": time_limit,
        "runs": runs
    };

    console.log("Starting fuzzing " + JSON.stringify(request, null, 2));

    let uuid = 0;

    const log = $("#log");
    // Periodical update function - uses the submission uuid
    let update_fun = function () {
        $.ajax({
            type: 'GET',
            url: "/submission/" + uuid,
            success: function (response) {
                console.log("Got update" + JSON.stringify(response, null, 2));

                if (response.success) {
                    if (response.state.finished) {
                        log.val(response.state.log);

                        if ("cases" in response.state) {
                            display(response.state.cases);
                        }
                        toggle_button(true);
                    } else {
                        log.val(response.state.log + "\n\nStill running...");
                        setTimeout(update_fun, 500);
                    }
                    // Scroll the textarea all the way down
                    log.scrollTop(log[0].scrollHeight);
                } else {
                    console.log("Unsuccessful update poll " + JSON.stringify(response, null, 2));
                    warn("Error in update request");
                    toggle_button(true);
                }
            },
            error: function (response) {
                warn("Error in update request.");
                toggle_button(true);
            }
        });
    };

    // Empty alerts
    $('.alerts').html("");

    // Empty result form
    log.val("");
    $("#cases").val("");
    $("#cases_tabs").empty();
    $("#cases_tab_contents").empty();

    // Initial submission request - if succeeded, will start periodical update requests via update fun
    $.ajax({
        type: 'POST',
        url: "/submission",
        contentType: 'application/json',
        data: JSON.stringify(request),
        success: function (response) {
            if (response.success) {
                toggle_button(false);
                $("#uuid").val(response.id);
                uuid = response.id;
                console.log("Started fuzzing with id " + response.id);
                update_fun();
            } else {
                warn("Could not start fuzzing " + response.errors);
                console.log("Could not start fuzzing " + JSON.stringify(response, null, 2))
                toggle_button(true);
            }
        },
        error: function (_) {
            warn("Error in fuzzing request.");
        }
    });
}

function problem_change() {
    const problem_name = $("#problem_name").val();
    const secret_list = $("#secret_name");
    secret_list.prop("disabled", true);
    secret_list.empty();

    if (!(problem_name in $("#problem_list").options)) {
        return;
    }
    $.get("/problem/" + problem_name + "/seeds", function (data) {
        if (data.success) {
            secret_list.prop("disabled", false);
            for (const seed of data.seeds) {
                secret_list.append(new Option(seed, seed));
            }

            for (const seed of data.seeds) {
                if (seed.startsWith("small")) {
                    secret_list.val(seed).change();
                    return
                }
            }
            if (data.seeds.length) {
                secret_list.val(data.seeds[0]).change();
            }
        }
    });
}

function add_problems(data) {
    const problem_list = $('#problem_list')
    let fragment = document.createDocumentFragment();
    for (const problem of data) {
        const option = document.createElement('option');
        option.textContent = problem;
        fragment.append(option);
    }
    problem_list.append(fragment);
    $('#problem_name').after(problem_list);
}

$(document).ready(function () {
    const source_input = $("#source");

    const source_change_listener = function (_) {
        source_change();
    };
    source_input.blur(source_change_listener);
    source_input.change(source_change_listener);

    $("#submit").click(function (event) {
        event.preventDefault();
        submit_problem();
    });
    $("#problem_name").blur(function (_) {
        problem_change();
    });
    problem_change();


    $("#source_lang").change(function (event) {
        const java_name = $("#java_name");
        if ($("#source_lang").val() === "java") {
            java_name.prop("disabled", false);
        } else {
            java_name.prop("disabled", true);
            java_name.val("");
        }
    });

    $.get('/problems', function (data) {
        if (data.success) {
            add_problems(data.problems);
        } else {
            warn("Failed to fetch problems")
        }
    });
});
