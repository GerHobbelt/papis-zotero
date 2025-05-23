"""Start a web server listening on port 23119. This server is
compatible with the `zotero connector`. This means that if zotero is
*not* running, you can have items from your web browser added directly
into papis.

"""

import http.server
import json
import logging
import re
import tempfile
import urllib.request
import urllib.error

from typing import Any, Dict

import papis.api
import papis.config
import papis.crossref
import papis.document

import papis_zotero.utils

logger = logging.getLogger("papis.{}".format(__name__))

ZOTERO_CONNECTOR_API_VERSION = 2
ZOTERO_VERSION = "5.0.25"
ZOTERO_PORT = 23119


def zotero_data_to_papis_data(item: Dict[str, Any]) -> Dict[str, Any]:
    from papis_zotero.sql import ZOTERO_TO_PAPIS_FIELD_MAP
    data = {}

    # NOTE: these are handled elsewhere
    item.pop("id", None)
    item.pop("attachments", None)

    # translate known zotero keys
    for key in ZOTERO_TO_PAPIS_FIELD_MAP:
        value = item.pop(key, None)
        if value is not None:
            data[ZOTERO_TO_PAPIS_FIELD_MAP[key]] = value

    # check zotero tags
    tags = item.pop("tags", None)
    if isinstance(tags, list):
        data["tags"] = " ".join(tags)

    data.update(item)

    # try to get information from Crossref as well
    doi = data.get("doi")
    if doi is not None:
        crossref_data = papis.crossref.doi_to_data(str(doi))
        crossref_data.pop("title", None)

        logger.info("Updating document with data from Crossref.")
        data.update(crossref_data)

    return data


class PapisRequestHandler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt: str, *args: Any) -> None:
        logger.info(fmt, args)

    def set_zotero_headers(self) -> None:
        self.send_header("X-Zotero-Version", ZOTERO_VERSION)
        self.send_header("X-Zotero-Connector-API-Version",
                         str(ZOTERO_CONNECTOR_API_VERSION))
        self.end_headers()

    def read_input(self) -> bytes:
        length = int(self.headers["content-length"])
        return self.rfile.read(length)

    def pong(self, POST: bool = True) -> None:  # noqa: N803
        # Pong must respond to ping on both GET and POST
        # It must accepts application/json and text/plain
        if not POST:  # GET
            logger.debug("Received a GET request.")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.set_zotero_headers()
            response = """\
            <!DOCTYPE html>
            <html>
                <head>
                    <title>Zotero Connector Server is Available</title>
                </head>
                <body>
                    Zotero Connector Server is Available
                </body>
            </html>
            """
        else:  # POST
            logger.debug("Received a POST request.")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.set_zotero_headers()
            response = json.dumps({"prefs": {"automaticSnapshots": True}})

        self.wfile.write(bytes(response, "utf8"))

    def papis_collection(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.set_zotero_headers()
        papis_library = papis.api.get_lib_name()
        response = json.dumps({
            "libraryID": 1,
            "libraryName": papis_library,
            # I'm not aware of a read-only papis mode
            "libraryEditable": True,
            # collection-level parameters
            "editable": True,
            # collection-level
            "id": None,
            # collection if collection, else library
            "name": papis_library
        })
        self.wfile.write(bytes(response, "utf8"))

    def add(self) -> None:
        logger.info("Adding paper from the Zotero Connector.")
        rawinput = self.read_input()
        data = json.loads(rawinput.decode("utf8"))

        for item in data["items"]:
            files = []
            if item.get("attachments") and len(item.get("attachments")) > 0:
                for attachment in item.get("attachments"):
                    mime = str(attachment.get("mimeType"))
                    logger.info("Checking attachment (mime %s).", mime)
                    if re.match(r".*pdf.*", mime):
                        url = attachment.get("url")
                        logger.info("Downloading PDF: '%s'.", url)
                        try:
                            pdfbuffer = urllib.request.urlopen(url).read()
                        except urllib.error.HTTPError:
                            logger.error(
                                "Error downloading PDF. You probably do not"
                                "have the rights to access the journal.")
                            continue

                        pdfpath = tempfile.mktemp(suffix=".pdf")
                        logger.info("Saving PDF: '%s'", pdfpath)

                        with open(pdfpath, "wb+") as fd:
                            fd.write(pdfbuffer)

                        if papis_zotero.utils.is_pdf(pdfpath):
                            files.append(pdfpath)
                        else:
                            logger.error(
                                "File retrieved does not appear to be a PDF. "
                                "Skipping!")
            else:
                logger.info("Document has no attachments.")

            papis_item = zotero_data_to_papis_data(item)
            logger.info("Adding paper to papis.")
            papis.commands.add.run(files, data=papis_item)

        self.send_response(201)  # Created
        self.set_zotero_headers()
        # return the JSON data back
        self.wfile.write(rawinput)

    def snapshot(self) -> None:
        logger.error("Snapshot not implemented!")
        self.send_response(201)
        self.set_zotero_headers()

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/connector/ping":
            self.pong()
        elif self.path == "/connector/getSelectedCollection":
            self.papis_collection()
        elif self.path == "/connector/saveSnapshot":
            self.snapshot()
        elif self.path == "/connector/saveItems":
            self.add()

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/connector/ping":
            self.pong(POST=False)
