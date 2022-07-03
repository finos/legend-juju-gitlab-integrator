#!/usr/bin/env python3
# Copyright 2021 Canonical Ltd.
# See LICENSE file for licensing details.

"""Module defining a Charm providing GitLab integration for FINOS Legend."""

import functools
import json
import logging
import traceback

import gitlab
from charms.finos_legend_gitlab_integrator_k8s.v0 import legend_gitlab
from ops import charm, framework, main, model

import utils

logger = logging.getLogger(__name__)

GITLAB_BASE_URL_FORMAT = "%(scheme)s://%(host)s:%(port)s"
GITLAB_SCHEME_HTTP = "http"
GITLAB_SCHEME_HTTPS = "https"
VALID_GITLAB_SCHEMES = [GITLAB_SCHEME_HTTP, GITLAB_SCHEME_HTTPS]

# TODO(aznashwan): consider making these configurable for people using LDAP
# https://gist.github.com/gpocentek/bd4c3fbf8a6ce226ebddc4aad6b46c0a
GITLAB_LOGIN_URL_FORMAT = "%(base_url)s/users/sign_in"
GITLAB_SIGNIN_URL_FORMAT = "%(base_url)s/users/sign_in"

GITLAB_OPENID_DISCOVERY_URL_FORMAT = "%(base_url)s/.well-known/openid-configuration"

GITLAB_REQUIRED_SCOPES = ["api", "openid", "profile"]

RELATION_NAME_GITLAB = "gitlab"
RELATION_NAME_SDLC = "legend-sdlc-gitlab"
RELATION_NAME_ENGINE = "legend-engine-gitlab"
RELATION_NAME_STUDIO = "legend-studio-gitlab"
ALL_LEGEND_RELATION_NAMES = [RELATION_NAME_SDLC, RELATION_NAME_ENGINE, RELATION_NAME_STUDIO]


def _safe_gitlab_call(op):
    """Decorator which catches GitLab API errors and returns a `model.BlockedStatus` instead."""

    @functools.wraps(op)
    def _inner(*args, **kwargs):
        try:
            return op(*args, **kwargs)
        except gitlab.exceptions.GitlabAuthenticationError as err:
            logger.exception("Exception occurred while attempting to list GitLab apps: %s", err)
            return model.BlockedStatus(
                "failed to authorize against gitlab, are the credentials correct?"
            )
        except gitlab.exceptions.GitlabError as err:
            logger.exception("Exception occurred while attempting to list GitLab apps: %s", err)
            if getattr(err, "response_code", 0) == 403:
                return model.BlockedStatus(
                    "gitlab refused access to the applications apis with a 403"
                    ", ensure the configured gitlab host can create "
                    "application or manuallly create one"
                )
            return model.BlockedStatus(
                "exception occurred while attempting to list existing GitLab apps"
            )
        except Exception:
            logger.error("Exception occured while listing GitLab apps: %s", traceback.format_exc())
            return model.BlockedStatus("failed to access gitlab api")

    return _inner


class LegendGitlabIntegratorCharm(charm.CharmBase):
    """Charm class which provides GitLab access to other Legend charms."""

    _stored = framework.StoredState()

    def __init__(self, *args):
        super().__init__(*args)

        self._set_stored_defaults()

        # General hooks:
        self.framework.observe(self.on.install, self._on_install)
        self.framework.observe(self.on.config_changed, self._on_config_changed)

        # GitLab charm relation events:
        self.framework.observe(
            self.on[RELATION_NAME_GITLAB].relation_joined, self._on_gitlab_relation_joined
        )
        self.framework.observe(
            self.on[RELATION_NAME_GITLAB].relation_changed, self._on_gitlab_relation_changed
        )

        # Legend component relation events:
        self.framework.observe(
            self.on[RELATION_NAME_SDLC].relation_joined,
            self._on_legend_sdlc_gitlab_relation_joined,
        )
        self.framework.observe(
            self.on[RELATION_NAME_SDLC].relation_changed,
            self._on_legend_sdlc_gitlab_relation_changed,
        )
        self.framework.observe(
            self.on[RELATION_NAME_SDLC].relation_broken,
            self._on_legend_sdlc_gitlab_relation_broken,
        )

        self.framework.observe(
            self.on[RELATION_NAME_ENGINE].relation_joined,
            self._on_legend_engine_gitlab_relation_joined,
        )
        self.framework.observe(
            self.on[RELATION_NAME_ENGINE].relation_changed,
            self._on_legend_engine_gitlab_relation_changed,
        )
        self.framework.observe(
            self.on[RELATION_NAME_ENGINE].relation_broken,
            self._on_legend_engine_gitlab_relation_broken,
        )

        self.framework.observe(
            self.on[RELATION_NAME_STUDIO].relation_joined,
            self._on_legend_studio_gitlab_relation_joined,
        )
        self.framework.observe(
            self.on[RELATION_NAME_STUDIO].relation_changed,
            self._on_legend_studio_gitlab_relation_changed,
        )
        self.framework.observe(
            self.on[RELATION_NAME_STUDIO].relation_broken,
            self._on_legend_studio_gitlab_relation_broken,
        )

        # Actions:
        self.framework.observe(self.on.get_redirect_uris_action, self._on_get_redirect_uris_action)
        self.framework.observe(
            self.on.get_legend_gitlab_params_action, self._on_get_legend_gitlab_params_action
        )

    def _set_stored_defaults(self) -> None:
        self._stored.set_default(log_level="DEBUG")
        self._stored.set_default(gitlab_client_id="")
        self._stored.set_default(gitlab_client_secret="")

    def _get_gitlab_scheme(self):
        scheme = self.model.config["api-scheme"]
        if scheme not in VALID_GITLAB_SCHEMES:
            raise ValueError(
                "Invalid GitLab scheme '%s'. Must be one of '%s'" % (scheme, VALID_GITLAB_SCHEMES)
            )

        return scheme

    def _get_gitlab_creds(self):
        relation = self.model.get_relation(RELATION_NAME_GITLAB)
        if relation:
            rel_data = relation.data[relation.app]
            data = json.loads(rel_data["credentials"])
            # data will have the same fields as below.
            return data

        # Return a dictionary with the same fields as the ones given by the gitlab relation.
        api_scheme = self._get_gitlab_scheme()
        return {
            "host": self.model.config["gitlab-host"],
            "port": self.model.config["gitlab-port"],
            "api-scheme": api_scheme,
            "access-token": self.model.config.get("access-token"),
        }

    def _get_gitlab_base_url(self):
        creds = self._get_gitlab_creds()
        return GITLAB_BASE_URL_FORMAT % {
            "scheme": creds["api-scheme"],
            "host": creds["host"],
            "port": creds["port"],
        }

    @property
    def _gitlab_client(self):
        creds = self._get_gitlab_creds()
        if not creds["access-token"]:
            return None
        return gitlab.Gitlab(
            self._get_gitlab_base_url(),
            private_token=creds["access-token"],
            # NOTE(aznashwan): we skip SSL verification for GitLabs with self-signed certs:
            ssl_verify=False,
        )

    def _get_gitlab_openid_discovery_url(self):
        return GITLAB_OPENID_DISCOVERY_URL_FORMAT % {"base_url": self._get_gitlab_base_url()}

    def _check_set_up_gitlab_application(self):
        """Sets up the GitLab application for the Legend deployment.

        If a GitLab App ID/secret was provided, this method will simply use those.
        Else, it will attempt to create a new application on GitLab.
        Either way, the client ID/secret of the app is set within stored state as it is only
        made available by the API on app creation.
        """
        gitlab_client_id = self.model.config["gitlab-client-id"]
        gitlab_client_secret = self.model.config["gitlab-client-secret"]
        if all([gitlab_client_id, gitlab_client_secret]):
            logger.info("Using pre-seeded Gitlab application ID/settings.")
            self._stored.gitlab_client_id = gitlab_client_id
            self._stored.gitlab_client_secret = gitlab_client_secret
            return None

        redirect_uris = self._get_legend_services_redirect_uris()
        if not redirect_uris:
            return model.BlockedStatus(
                "cannot create gitlab app without all legend services related"
            )

        # Check GitLab client available:
        gitlab_client = self._gitlab_client
        if not gitlab_client:
            return model.BlockedStatus("awaiting gitlab server configuration or relation")

        # NOTE(aznashwan): GitLab.com has disabled the application APIs:
        existing_apps = _safe_gitlab_call(gitlab_client.applications.list)()
        if isinstance(existing_apps, model.BlockedStatus):
            return existing_apps

        # Check app name is available:
        # We're prefixing the gitlab application name with this charm's app name, so we know
        # that we're the its creators. We need this to make sure we don't update any existing
        # application that isn't related to us.
        app_name = "%s - %s" % (self.app.name, self.model.config["application-name"])

        # We need to update the Callback URIs if they've changed (e.g.: changed external-hostname)
        matches = [app for app in existing_apps if app.application_name == app_name]
        if matches:
            app = matches[0]
            if app.callback_url == redirect_uris:
                # Nothing to do, the Callback URIs already match.
                return

        # TODO(aznashwan): make app trusted by default:
        # https://github.com/finos/legend/blob/master/installers/docker-compose/legend/scripts/setup-gitlab.sh#L36-L42
        app = None
        app_properties = {
            "name": app_name,
            "scopes": " ".join(GITLAB_REQUIRED_SCOPES),
            "redirect_uri": redirect_uris,
        }
        logger.info(
            "Attempting to create new GitLab application with the following properties: %s",
            app_properties,
        )
        try:
            app = gitlab_client.applications.create(app_properties)
        except Exception as ex:
            logger.exception(ex)
            return model.BlockedStatus("failed to create application on gitlab")

        # NOTE(aznashwan): the client ID and secret are only available from the API
        # call which created the application, so we store them inside the charm:
        self._stored.gitlab_client_id = app.application_id
        self._stored.gitlab_client_secret = app.secret

    def _get_legend_redirect_uris_from_relation(self, relation_name):
        relation = None
        try:
            relation = self.model.get_relation(relation_name)
            if not relation:
                return None
            gitlab_consumer = legend_gitlab.LegendGitlabConsumer(self, relation_name)
            return gitlab_consumer.get_legend_redirect_uris(relation.id)
        except model.TooManyRelatedAppsError:
            logger.error("this operator does not support multiple %s relations" % (relation_name))
            return None
        except model.ModelError as ex:
            logger.error(
                "Encountered an error while getting the '%s' redirect URIs: %s", relation_name, ex
            )
            return None

    def _get_legend_services_redirect_uris(self):
        """Returns a string containing the service URLs in the correct order.

        Returns an empty string if not all Legend services are related.
        """
        relation_names = [
            # NOTE(aznashwan): order of these is important:
            RELATION_NAME_ENGINE,
            RELATION_NAME_SDLC,
            RELATION_NAME_STUDIO,
        ]

        # NOTE: it is okay for a service to not have any redirect URIs
        # (i.e. empty string), but not okay for them to not be set (i.e. None):
        redirect_uris = ""
        for relation_name in relation_names:
            uris = self._get_legend_redirect_uris_from_relation(relation_name)
            if uris is None:
                logger.warning("Missing redirect URIs for '%s' relation.", relation_name)
                return ""
            redirect_uris = "%s\n%s" % (redirect_uris, "\n".join(uris))
        redirect_uris = redirect_uris.strip("\n")

        return redirect_uris

    def _check_legend_services_relations_status(self):
        """Checks whether all the required Legend services are currently related.

        Returns None if all the relations are present, or a `model.BlockedStatus`
        with a relevant message otherwise.
        """
        relation_names = [RELATION_NAME_SDLC, RELATION_NAME_ENGINE, RELATION_NAME_STUDIO]
        # NOTE(aznashwan): it is acceptable for a service to have no redirect
        # URIs (empty string), but not None:
        missing = [
            rel
            for rel in relation_names
            if self._get_legend_redirect_uris_from_relation(rel) is None
        ]
        if missing:
            return model.BlockedStatus(
                "missing following legend relations: %s" % ", ".join(missing)
            )
        return None

    def _get_gitlab_host_cert_b64(self):
        creds = self._get_gitlab_creds()
        host = creds["host"]
        port = creds["port"]
        if any([not param for param in [host, port]]):
            return model.BlockedStatus(
                "both a 'gitlab-host' and 'gitlab-port' config options / relation data are required"
            )

        try:
            return utils.get_gitlab_host_cert_b64(host, port)
        except Exception:
            return model.BlockedStatus(
                "failed to retrieve SSL cert for GitLab host '%s:%d'. SSL is required "
                "for the GitLab to be usable by the Legend components" % (host, port)
            )

    def _get_gitlab_relation_data(self):
        if not all([self._stored.gitlab_client_id, self._stored.gitlab_client_secret]):
            return model.BlockedStatus("awaiting gitlab server configuration or relation")

        creds = self._get_gitlab_creds()
        host = creds["host"]
        port = creds["port"]
        scheme = creds["api-scheme"]
        rel_data = {
            "gitlab_host": host,
            "gitlab_port": port,
            "gitlab_scheme": scheme,
            "client_id": self._stored.gitlab_client_id,
            "client_secret": self._stored.gitlab_client_secret,
            "openid_discovery_url": self._get_gitlab_openid_discovery_url(),
        }

        cert_b64 = ""
        if scheme == GITLAB_SCHEME_HTTPS:
            cert_b64 = self._get_gitlab_host_cert_b64()
            if isinstance(cert_b64, model.BlockedStatus):
                return cert_b64
        rel_data["gitlab_host_cert_b64"] = cert_b64

        return rel_data

    def _set_legend_gitlab_data_in_relation(
        self, relation_name, gitlab_relation_data, validate_creds=True
    ):
        """Sets the provided GitLab data into the given relation.

        Returns a `model.BlockedStatus` is something goes wrong, else None.
        """
        relation = None
        try:
            relation = self.model.get_relation(relation_name)
        except model.TooManyRelatedAppsError:
            return model.BlockedStatus(
                "this operator does not support multiple %s relations" % (relation_name)
            )
        if not relation:
            logger.info("No '%s' relation present", relation_name)
            return None

        try:
            legend_gitlab.set_legend_gitlab_creds_in_relation_data(
                relation.data[self.app], gitlab_relation_data, validate_creds=validate_creds
            )
        except ValueError as ex:
            logger.warning("Error occurred while setting GitLab creds relation data: %s", str(ex))
            return model.BlockedStatus(
                "failed to set gitlab credentials in %s relation" % (relation_name)
            )
        return None

    def _set_gitlab_data_in_all_relations(self, gitlab_relation_data, validate_creds=True):
        """Sets the provided GitLab data into all the relations with the Legend services.

        Returns a `model.BlockedStatus` is something goes wrong, else None.
        """
        for relation_name in ALL_LEGEND_RELATION_NAMES:
            blocked = self._set_legend_gitlab_data_in_relation(
                relation_name, gitlab_relation_data, validate_creds=validate_creds
            )
            if blocked:
                return blocked

    def _update_charm_status(self):
        """Updates the status of the charm as well as all relations."""
        possible_blocked_status = self._check_legend_services_relations_status()
        if possible_blocked_status:
            self.unit.status = possible_blocked_status
            return

        possible_blocked_status = self._check_set_up_gitlab_application()
        if possible_blocked_status:
            self.unit.status = possible_blocked_status
            return

        gitlab_relation_data = self._get_gitlab_relation_data()
        if isinstance(gitlab_relation_data, model.BlockedStatus):
            self.unit.status = gitlab_relation_data
            return

        # propagate the relation data:
        possible_blocked_status = self._set_gitlab_data_in_all_relations(
            gitlab_relation_data, validate_creds=False
        )
        if possible_blocked_status:
            self.unit.status = possible_blocked_status
            return

        self.unit.status = model.ActiveStatus()

    def _on_install(self, event: charm.InstallEvent):
        self._update_charm_status()

    def _on_config_changed(self, _) -> None:
        self._update_charm_status()

    def _on_gitlab_relation_joined(self, event: charm.RelationJoinedEvent):
        pass

    def _on_gitlab_relation_changed(self, event: charm.RelationChangedEvent) -> None:
        self._update_charm_status()

    def _on_legend_sdlc_gitlab_relation_joined(self, event: charm.RelationJoinedEvent) -> None:
        pass

    def _on_legend_sdlc_gitlab_relation_changed(self, event: charm.RelationChangedEvent) -> None:
        self._update_charm_status()

    def _on_legend_sdlc_gitlab_relation_broken(self, event: charm.RelationBrokenEvent) -> None:
        self._update_charm_status()

    def _on_legend_engine_gitlab_relation_joined(self, event: charm.RelationJoinedEvent) -> None:
        pass

    def _on_legend_engine_gitlab_relation_changed(self, event: charm.RelationChangedEvent) -> None:
        self._update_charm_status()

    def _on_legend_engine_gitlab_relation_broken(self, event: charm.RelationBrokenEvent) -> None:
        self._update_charm_status()

    def _on_legend_studio_gitlab_relation_joined(self, event: charm.RelationJoinedEvent) -> None:
        pass

    def _on_legend_studio_gitlab_relation_changed(self, event: charm.RelationChangedEvent) -> None:
        self._update_charm_status()

    def _on_legend_studio_gitlab_relation_broken(self, event: charm.RelationBrokenEvent) -> None:
        self._update_charm_status()

    def _on_get_redirect_uris_action(self, event: charm.ActionEvent) -> None:
        redirect_uris = self._get_legend_services_redirect_uris()
        if not redirect_uris:
            raise Exception("Need to have all Legend services related to return redirect URIs.")
        event.set_results({"result": redirect_uris})

    def _on_get_legend_gitlab_params_action(self, event: charm.ActionEvent) -> None:
        params = self._get_gitlab_relation_data()
        if isinstance(params, (model.BlockedStatus, model.WaitingStatus)):
            raise Exception("No GitLab configuration currently present.")
        event.set_results({"result": {k.replace("_", "-"): v for k, v in params.items()}})


if __name__ == "__main__":
    main.main(LegendGitlabIntegratorCharm)
