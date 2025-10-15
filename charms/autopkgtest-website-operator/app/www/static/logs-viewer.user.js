// ==UserScript==
// @name         Autopkgtest Logs Enhancer
// @namespace    http://tampermonkey.net/
// @version      2024-06-19
// @description  Does some processing to beautify the tests logs on https://autopkgtest.ubuntu.com
// @author       Corentin -Cajuteq- Jacquet, Point Vermeil, https://pointvermeil.fr/
// @author       Florent 'Skia' Jacquet <florent.jacquet@canonical.com>
// @match        objectstorage.prodstack5.canonical.com/*
// @match        autopkgtest.ubuntu.com/*
// @icon         data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAADAAAAAwCAMAAABg3Am1AAAAe1BMVEXpVCD////99PLqXDDpVSPpVyb//fz++vnpWCn+9/b41M7ra0vvl4Xyqp398u/pWiz2yMDuj3zxpJXyrqL64t376eX53NbrcVPqZD70ua7xp5nwno3tgWr1wLf86ufzs6jsdlrqXzb30Mjvk4DqZkL2xb3th3LseV/sdFe/EwyjAAABzUlEQVRIie1UyXaDMAyUMdhgdsIaQghJQ/v/X1gbYxqCSPtee6xOIM9Yo8UC+Ldfm+06P4E5fpqFrQ30kEdFUn136ak7c0JID3VJlFnjS/womIbVkBBtUb0Ppx2fUQJ880mOu3hfiyDsXGYQGDxJdvHFdF4cLqo23xOGfBIfxvq3WiQdcLx9n65/XxxXk7QPLka4FB4huf+gcIpIvACqxMYYTh2Iy6Oj7s5eJE4QC5Yiiga6vcYdZEK11Caez5xjznmeoZFbqYs/DYiju8o6jEEjeRSufdlcEBZgIZKp8yv9pSl5iRHUdcUqNvUMwYoRwkk1Y3VALUOIMIIvB9iiK9ciSSB4qLaEgM1JIw0CSFXo9XTYzcRgPYaHgzzKnwpu30QUCfR+AIGPuIuOpDRf1XDbICem1fENIzSq3MPGXZ0tRu5rnf5Yyben3sl9g9cDwNoHx1hywvIbxCXxPhBCpbrn3Zb/VLeft/DOGzS5TJWWh/NypMXXWuopSrA7PX/jVPJ0WRr7a8m963bnfTpAuBC6XQI4jVktzfJE1PcLS2flV/gwXPZ6GdOwmDM1u3jz/jeZpF1u8R4GoWuAdn8T503CnOBaihZ7Uf/21/YJkRUUt/HLfJYAAAAASUVORK5CYII=
// @grant        GM_addStyle
// ==/UserScript==

(function() {
    'use strict';

    addEventListener("load", main)

    function main() {
        add_surrounding();
    }

    function add_surrounding() {
        const css = `
body {
  margin: 0;
}
.button-bar {
  position: sticky;
  top: 0;
  background: #222;
  height: 3em;
}

.button-bar button {
  margin: 0.5em;
}

/* -----------------------------------------------------------------
 * Log viewer
 * ----------------------------------------------------------------- */
.log-view .log-divider {
  background-color: #2e3436;
  padding: 0.5em;
  top: 4em;
  position: sticky;
}

.log-view .log-divider a {
  color: #66ff66;
  font-weight: bold;
  padding-left: 5em;
  text-decoration: none;
}

.log-view .log-divider a:hover {
  text-decoration: underline;
}


.log-view .log-divider-fail a {
  color: #ff6666;
}

.log-view .log-divider .log-open {
  display: none;
}

.log-view .line-number {
  display: inline-block;
  padding: 0.2em;
  margin-right: 1em;
  width: 5em;
  text-align: right;
  background-color: #555753;
  color: #eeeeec;
  user-select: none;
  text-decoration: none;
}

.log-view a.line-number:hover {
  background-color: #ffc;
  color: #888a85;
  text-decoration: underline;
}

.log-view {
  font-family: monospace;
  background-color: #2e3436;
  color: #eeeeec;
  white-space: pre-wrap;
  word-wrap: break-word;
}

.log-section > .line-number:first-child + .log-line,
.log-section > .line-number:first-child {
  padding-top: 0.5em;
}

.log-section .log-line:last-child {
  padding-bottom: 0.5em;
}

.log-section {
  white-space: pre-wrap;
}

    `
        GM_addStyle(css)
        const rawlog = document.getElementsByTagName("pre")[0]
        const newDiv = document.createElement("div");
        newDiv.className = "log-view";

        const buttonBar = document.createElement("div");
        buttonBar.className = "button-bar";

        const foldAllButton = document.createElement("button");
        foldAllButton.innerText = "fold all"
        foldAllButton.addEventListener('click', () => {
            var sections = document.getElementsByClassName("log-section")
            for (var section of sections) {
                section.style.display = "none";
            }
            var sectionsArrowLeft = document.getElementsByClassName("log-open")
            for (var sectionArrowLeft of sectionsArrowLeft) {
                sectionArrowLeft.style.display = "inherit";
            }
            var sectionsArrowDown = document.getElementsByClassName("log-close")
            for (var sectionArrowDown of sectionsArrowDown) {
                sectionArrowDown.style.display = "none";
            }
        })
        buttonBar.appendChild(foldAllButton)

        const unfoldAllButton = document.createElement("button");
        unfoldAllButton.innerText = "unfold all"
        unfoldAllButton.addEventListener('click', () => {
            var sections = document.getElementsByClassName("log-section")
            for (var section of sections) {
                section.style.display = "inherit";
            }
            var sectionsArrowLeft = document.getElementsByClassName("log-open")
            for (var sectionArrowLeft of sectionsArrowLeft) {
                sectionArrowLeft.style.display = "none";
            }
            var sectionsArrowDown = document.getElementsByClassName("log-close")
            for (var sectionArrowDown of sectionsArrowDown) {
                sectionArrowDown.style.display = "inherit";
            }
        })
        buttonBar.appendChild(unfoldAllButton)

        const linedLog = rawlog.innerText.split("\n");

        function toggleLogSection(section) {
            var arrows = section.getElementsByClassName("log-toggle")
            if (arrows[0].style.display == "none") {
                arrows[0].style.display = "inline";
                arrows[1].style.display = "none";
                section.parentElement.nextSibling.style.display = "none";

            } else {
                arrows[0].style.display = "none";
                arrows[1].style.display = "inline";
                section.parentElement.nextSibling.style.display = "inherit";

            }
        };


        var logSection
        var logDivider

        var logSectionNb = 0;
        function createSection(text){
            logSectionNb+=1;
            logDivider = document.createElement("div");
            logDivider.className = "log-divider log-divider-";
            newDiv.appendChild(logDivider);

            const dividerAnchor = document.createElement("a");
            dividerAnchor.href = "#S"+logSectionNb;
            dividerAnchor.onclick = function() {return toggleLogSection(this)}
            const dividerArrowLeft = document.createElement("span");
            dividerArrowLeft.className = "log-toggle log-open"
            dividerArrowLeft.style.display = "none";
            const dividerArrowDown = document.createElement("span");
            dividerArrowDown.className = "log-toggle log-close"
            dividerArrowDown.style.display = "inline";
            const dividerArrowLeftContent = document.createTextNode(" ▸ ");
            dividerArrowLeft.appendChild(dividerArrowLeftContent)
            const dividerArrowDownContent = document.createTextNode(" ▾ ");
            dividerArrowDown.appendChild(dividerArrowDownContent)
            const dividerName = document.createElement("span");
            dividerName.className = "log-section-name"
            const dividerContent = document.createTextNode(text);
            dividerName.appendChild(dividerContent)

            dividerAnchor.appendChild(dividerArrowLeft)
            dividerAnchor.appendChild(dividerArrowDown)
            dividerAnchor.appendChild(dividerName)
            logDivider.appendChild(dividerAnchor);

            logSection = document.createElement("div");
            logSection.className = "log-section";
            newDiv.appendChild(logSection);
            return dividerAnchor;
        };

        // This is used to store the current session, and auto-fold some of them when they're done
        var s;

        for (const line in linedLog) {
            switch (true) {
                case /^ *[0-9]+s autopkgtest.*: starting date/.test(linedLog[line]):
                    s = createSection("start run");
                    break;
                case /^ *[0-9]+s autopkgtest.*: @@@@@@@@@@@@@@@@@@@@ test bed setup/.test(linedLog[line]):
                    s = createSection("test bed setup");
                    toggleLogSection(s); // fold section by default
                    break;
                case /^ *[0-9]+s autopkgtest.*: @@@@@@@@@@@@@@@@@@@@ apt-source/.test(linedLog[line]):
                    s = createSection("apt-source");
                    toggleLogSection(s); // fold section by default
                    break;
                case /^ *[0-9]+s autopkgtest.*: test .*/.test(linedLog[line]):
                    var group = linedLog[line].match(/autopkgtest \[.*\]: test (.*): (.*)/)
                    if (group[2].startsWith("preparing testbed")) {
                        s = createSection("test '" + group[1] + "': preparing testbed");
                        toggleLogSection(s); // fold section by default
                    } else if (group[2].startsWith("[---")) {
                        s = createSection("test '" + group[1] + "': test run");
                        toggleLogSection(s); // fold section by default
                    } else if (group[2].startsWith(" - - - - - - - - - - results - -")) {
                        s = createSection("test '" + group[1] + "': test results");
                    }
                    break;
                case /^ *[0-9]+s autopkgtest.*: @@@@@@@@@@@@@@@@@@@@ summary/.test(linedLog[line]):
                    s = createSection("summary");
                    break;
                default:
                    break;
            }

            const lineAnchor = document.createElement("a");
            lineAnchor.id = line;
            lineAnchor.className = "line-number";
            lineAnchor.href = "#"+line;

            const lineNb = document.createTextNode(line);
            lineAnchor.appendChild(lineNb)
            logSection.appendChild(lineAnchor);


            const newContent = document.createTextNode(linedLog[line] + '\n');
            logSection.appendChild(newContent);


        }

        document.body.insertBefore(buttonBar, rawlog);
        document.body.insertBefore(newDiv, rawlog);
        rawlog.remove();
    }
})();
