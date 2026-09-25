Github trivia
=============

We have github actions to publish and release the charms when a PR is accepted.

They depend on a CHARMHUB_TOKEN, in the repo's secrets.

The previous one was not working, so P-EB put one he emitted with his account.

Charmhub token generation procedure
-----------------------------------

.. code-block:: bash
    charmcraft login --export ${HOME}/charm_token.txt --charm ubuntu-autopkgtest-dispatcher --charm ubuntu-autopkgtest-janitor --charm ubuntu-autopkgtest-website --permission=package-view --permission=package-manage-revisions --permission=package-manage-releases --channel "latest/edge" --ttl "$((365 * 24 * 60 * 60))"

Extract the content and put it in the repo's secret.

Charmhub addons
---------------

Script to list tokens:

.. code-block:: bash

   #!/bin/bash

   if [ -z "${CHARMHUB_TOK}" ]; then
       echo "Set CHARMHUB_TOK."
       exit 1
   else
       curl -H "Authorization: Macaroon "$(echo -n ${CHARMHUB_TOK}|base64 -d)"" https://api.charmhub.io/v1/tokens | jq .
   fi

To run with::

  CHARMHUB_TOK="$(cat "${HOME}/charm_token.txt")" ./script_name

Script to revoke a token:

.. code-block:: bash

   #!/bin/bash

   if [ -z "${CHARMHUB_TOK}" ]; then
       echo "Set CHARMHUB_TOK."
       exit 1
   else
       curl -H "Authorization: Macaroon "$(echo -n ${CHARMHUB_TOK}|base64 -d)"" https://api.charmhub.io/v1/tokens/revoke -H "Content-Type: application/json" -d '{"session-id": "'${1}'"}'|jq .
   fi

To run with::

   CHARMHUB_TOK="$(cat "${HOME}/charm_token.txt")" ./script_name <value got from the listing script in session-id>
