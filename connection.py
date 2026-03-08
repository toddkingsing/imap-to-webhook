import logging

logger = logging.getLogger("imap-to-webhook")


class IMAPClient:
    def __init__(self, config):
        self._config = config
        self._connect(config)

    def _connect(self, config):
        transport = config["imap"]["transport"]
        hostname = config["imap"]["hostname"]
        port = config["imap"]["port"]
        timeout = config["imap"].get("timeout", 60)
        new_client = transport(host=hostname, port=port, timeout=timeout)
        username = config["imap"]["username"]
        password = config["imap"]["password"]
        logger.info("Connecting to mail server: %s", hostname)
        if username and password:
            login = new_client.login(username, password)
            if login[0] != "OK":
                try:
                    new_client.logout()
                except Exception:
                    pass
                raise Exception("Unable to login", login)
        logger.info("Logged in as %s", username)
        select_folder = new_client.select(config["imap"]["inbox"])
        if select_folder[0] != "OK":
            try:
                new_client.logout()
            except Exception:
                pass
            raise Exception("Unable to select folder", select_folder)
        self.client = new_client
        self.on_success = config["imap"].get("on_success", "move")
        self.noop_flag = config["imap"].get("noop_flag", r"\Seen")

    def reconnect(self):
        """Close existing connection and re-establish."""
        hostname = self._config["imap"]["hostname"]
        logger.info("Attempting IMAP reconnect to %s", hostname)
        try:
            self.client.logout()
        except Exception:
            pass
        self._connect(self._config)
        logger.info("IMAP reconnected successfully to %s", hostname)

    def get_mail_ids(self):
        if self.on_success == "noop":
            if self.noop_flag == r"\Seen":
                result_search, data = self.client.uid("SEARCH", "UNSEEN")
            else:
                result_search, data = self.client.uid(
                    "SEARCH", "UNKEYWORD", self.noop_flag
                )
        else:
            result_search, data = self.client.uid("SEARCH", "ALL")
        if result_search != "OK":
            raise Exception(
                f"Search failed: status={result_search}, data={data}"
            )
        if not data or data[0] is None:
            return []
        return data[0].decode("utf-8").split()

    def fetch(self, msg_id):
        result_fetch, data = self.client.uid("FETCH", "{} (BODY.PEEK[])".format(msg_id))
        if result_fetch != "OK":
            raise Exception(
                f"Fetch failed for msg {msg_id}: status={result_fetch}, data={data}"
            )
        if not data or not data[0]:
            raise Exception("Empty fetch response for msg {}".format(msg_id))
        raw = data[0]
        if not isinstance(raw, (list, tuple)) or len(raw) < 2:
            raise Exception(
                "Unexpected fetch response format for msg {}: {!r}".format(
                    msg_id, type(raw).__name__
                )
            )
        return raw[1]

    def connection_close(self):
        try:
            self.client.close()
            logger.info("Connection closed")
        except Exception:
            pass
        try:
            self.client.logout()
            logger.info("Logged out")
        except Exception:
            pass

    def move(self, msg_id, folder):
        logger.info("Going to move %s to %s", msg_id, folder)
        self.copy(msg_id, folder)
        try:
            self.mark_delete(msg_id)
        except Exception as e:
            logger.error(
                "Copied msg %s to %s but mark_delete failed: %s. "
                "Message may exist in both folders.",
                msg_id,
                folder,
                e,
            )
            raise

    def mark_delete(self, msg_id):
        # Sets \Deleted flag — message is expunged on CLOSE/EXPUNGE
        logger.info("Going to mark as deleted %s", msg_id)
        delete_result, _ = self.client.uid("STORE", msg_id, "+FLAGS", r"(\Deleted)")
        if delete_result != "OK":
            raise Exception("Failed to mark as deleted msg {}".format(msg_id))

    def mark_seen(self, msg_id):
        # Sets \Seen flag — message appears as "read" in mail clients
        logger.info("Marking message %s as seen", msg_id)
        result, _ = self.client.uid("STORE", msg_id, "+FLAGS", r"(\Seen)")
        if result != "OK":
            raise Exception("Failed to mark as seen msg {}".format(msg_id))

    def mark_processed(self, msg_id):
        """Mark message with the configured noop flag.

        Supports RFC 3501 system flags (\Seen, \Answered, \Flagged, \Deleted,
        \Draft) and custom keywords ($WebhookProcessed, etc.).
        Custom keywords require server PERMANENTFLAGS to include \*.
        """
        logger.info(
            "Marking message %s as processed (flag: %s)", msg_id, self.noop_flag
        )
        if self.noop_flag == r"\Seen":
            flag_str = r"(\Seen)"
        else:
            flag_str = f"({self.noop_flag})"
        result, _ = self.client.uid("STORE", msg_id, "+FLAGS", flag_str)
        if result != "OK":
            raise Exception(
                f"Failed to mark msg {msg_id} as processed with flag {self.noop_flag}"
            )

    def copy(self, msg_id, folder):
        logger.info("Going to copy %s to %s", msg_id, folder)
        copy_result, data = self.client.uid("COPY", msg_id, folder)
        if copy_result != "OK":
            logger.error("Copy failed: %s %s", copy_result, data)
            raise Exception("Failed to copy msg {} to {}".format(msg_id, folder))

    def expunge(self):
        result, _ = self.client.expunge()
        if result != "OK":
            logger.warning("Expunge failed, deleted messages may not be purged")
