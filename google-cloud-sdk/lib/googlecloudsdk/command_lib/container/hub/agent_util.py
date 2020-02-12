# -*- coding: utf-8 -*- #
# Copyright 2019 Google LLC. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Utils for GKE Hub commands."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

from googlecloudsdk.api_lib.container.hub import gkehub_api_adapter
from googlecloudsdk.api_lib.container.hub import gkehub_api_util
from googlecloudsdk.command_lib.container.hub import kube_util
from googlecloudsdk.command_lib.projects import util as p_util
from googlecloudsdk.core import exceptions
from googlecloudsdk.core import log
from googlecloudsdk.core import properties
from googlecloudsdk.core.util import encoding
from googlecloudsdk.core.util import files

# The name of the Deployment for the runtime Connect agent.
RUNTIME_CONNECT_AGENT_DEPLOYMENT_NAME = 'gke-connect-agent'

# The app label applied to Pods for the install agent workload.
AGENT_INSTALL_APP_LABEL = 'gke-connect-agent-installer'

# The name of the Connect agent install deployment.
AGENT_INSTALL_DEPLOYMENT_NAME = 'gke-connect-agent-installer'

# The name of the Secret that stores the Google Cloud Service Account
# credentials. This is also the basename of the only key in that secret's Data
# map, the filename '$GCP_SA_KEY_SECRET_NAME.json'.
GCP_SA_KEY_SECRET_NAME = 'creds-gcp'

# The name of the secret that will store the Docker private registry
# credentials, if they are provided.
IMAGE_PULL_SECRET_NAME = 'connect-image-pull-secret'

CONNECT_RESOURCE_LABEL = 'hub.gke.io/project'

DEFAULT_NAMESPACE = 'gke-connect'

MANIFEST_SAVED_MESSAGE = """\
Manifest saved to [{0}]. Please apply the manifest to your cluster with \
`kubectl apply -f {0}`. You must have `cluster-admin` privilege in order to \
deploy the manifest.

**This file contains sensitive data; please treat it with the same discretion \
as your service account key file.**"""

CREDENTIAL_SECRET_TEMPLATE = """\
apiVersion: v1
kind: Secret
metadata:
  name: {gcp_sa_key_secret_name}
  namespace: {namespace}
data:
  {gcp_sa_key_secret_name}.json: {gcp_sa_key}
"""

NAMESPACE_TEMPLATE = """\
apiVersion: v1
kind: Namespace
metadata:
  name: {namespace}
  labels:
    {connect_resource_label}: {project_id}
"""

INSTALL_ALPHA_TEMPLATE = """\
apiVersion: v1
kind: ConfigMap
metadata:
  name: user-config
  namespace: {namespace}
data:
  project_id: "{project_id}"
  project_number: "{project_number}"
  membership_name: "{membership_name}"
  proxy: "{proxy}"
  image: "{image}"
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: {project_id}-gke-connect-agent-role-binding
  labels:
    {connect_resource_label}: {project_id}
subjects:
- kind: ServiceAccount
  name: default
  namespace: {namespace}
roleRef:
  kind: ClusterRole
  name: cluster-admin
  apiGroup: rbac.authorization.k8s.io
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {agent_install_deployment_name}
  namespace: {namespace}
  labels:
    app: {agent_install_app_label}
spec:
  selector:
    matchLabels:
      app: {agent_install_app_label}
  template:
    metadata:
      labels:
        app: {agent_install_app_label}
    spec:
      containers:
      - name: connect-agent-installer
        image: {image}
        command:
          - gkeconnect_bin/bin/gkeconnect_agent
          - --install
          - --sleep-after-install
          - --config
          - user-config
        imagePullPolicy: Always
        env:
        - name: MY_POD_NAMESPACE
          valueFrom:
            fieldRef:
              fieldPath: metadata.namespace
"""

# The manifest used to deploy the Connect agent install workload and its
# supporting components.
#
# Note that the deployment must be last: kubectl apply deploys resources in
# manifest order, and the deployment depends on other resources; and the
# imagePullSecrets template below is appended to this template if image
# pull secrets are required.
INSTALL_MANIFEST_TEMPLATE = NAMESPACE_TEMPLATE + """\
---
""" + CREDENTIAL_SECRET_TEMPLATE + """\
---
""" + INSTALL_ALPHA_TEMPLATE

# The secret that will be installed if a Docker registry credential is provided.
# This is appended to the end of INSTALL_MANIFEST_TEMPLATE.
IMAGE_PULL_SECRET_TEMPLATE = """\
apiVersion: v1
kind: Secret
metadata:
  name: {name}
  namespace: {namespace}
  labels:
    {connect_resource_label}: {project_id}
data:
  .dockerconfigjson: {image_pull_secret}
type: kubernetes.io/dockerconfigjson"""


def _PurgeAlphaInstaller(kube_client, namespace, project_id):
  """Purge the Alpha installation resources if exists.

  Args:
    kube_client: Kubernetes client to operate on the cluster.
    namespace: the namespace of Alpha installation.
    project_id: the GCP project ID.

  Raises:
    exceptions.Error: if Alpha resources deletion failed.
  """
  project_number = p_util.GetProjectNumber(project_id)
  err = kube_client.Delete(INSTALL_ALPHA_TEMPLATE.format(
      namespace=namespace,
      connect_resource_label=CONNECT_RESOURCE_LABEL,
      project_id=project_id,
      project_number=project_number,
      membership_name='',
      proxy='',
      image='',
      gcp_sa_key='',
      gcp_sa_key_secret_name=GCP_SA_KEY_SECRET_NAME,
      agent_install_deployment_name=AGENT_INSTALL_DEPLOYMENT_NAME,
      agent_install_app_label=AGENT_INSTALL_APP_LABEL
      ))
  if err:
    if 'NotFound' not in err:
      raise exceptions.Error('failed to delete Alpha installation: {}'.format(
          err))


def _GetConnectAgentOptions(args, upgrade, namespace, image_pull_secret_data,
                            membership_ref):
  return gkehub_api_adapter.ConnectAgentOption(
      name=args.CLUSTER_NAME,
      proxy=args.proxy or '',
      namespace=namespace,
      is_upgrade=upgrade,
      version=args.version or '',
      registry=args.docker_registry or '',
      image_pull_secret_content=image_pull_secret_data or '',
      membership_ref=membership_ref)


def _GenerateManifest(args, service_account_key_data, image_pull_secret_data,
                      upgrade, membership_ref, release_track=None):
  """Generate the manifest for connect agent from API.

  Args:
    args: arguments of the command.
    service_account_key_data: The contents of a Google IAM service account JSON
      file.
    image_pull_secret_data: The image pull secret content to use for private
      registries.
    upgrade: if this is an upgrade operation.
    membership_ref: The membership associated with the connect agent in the
      format of `projects/[PROJECT]/locations/global/memberships/[MEMBERSHIP]`
    release_track: the release_track used in the gcloud command,
      or None if it is not available.

  Returns:
    The full manifest to deploy the connect agent resources.
  """
  api_version = gkehub_api_util.GetApiVersionForTrack(release_track)
  adapter = gkehub_api_adapter.NewAPIAdapter(api_version)
  connect_agent_ref = _GetConnectAgentOptions(args,
                                              upgrade,
                                              DEFAULT_NAMESPACE,
                                              image_pull_secret_data,
                                              membership_ref)
  manifest_resources = adapter.GenerateConnectAgentManifest(connect_agent_ref)
  delimeter = '---\n'
  full_manifest = ''

  for resource in manifest_resources:
    full_manifest = full_manifest + resource['manifest'] + delimeter

  # Append creds secret.
  full_manifest = full_manifest + CREDENTIAL_SECRET_TEMPLATE.format(
      namespace=DEFAULT_NAMESPACE,
      gcp_sa_key_secret_name=GCP_SA_KEY_SECRET_NAME,
      gcp_sa_key=encoding.Decode(service_account_key_data, encoding='utf8'))
  return full_manifest


def DeployConnectAgent(kube_client, args,
                       service_account_key_data,
                       image_pull_secret_data,
                       membership_ref, release_track=None):
  """Deploys the GKE Connect agent to the cluster.

  Args:
    kube_client: A Kubernetes Client for the cluster to be registered.
    args: arguments of the command.
    service_account_key_data: The contents of a Google IAM service account JSON
      file
    image_pull_secret_data: The contents of image pull secret to use for
      private registries.
    membership_ref: The membership should be associated with the connect agent
      in the format of
      `project/[PROJECT]/location/global/memberships/[MEMBERSHIP]`.
    release_track: the release_track used in the gcloud command,
      or None if it is not available.
  Raises:
    exceptions.Error: If the agent cannot be deployed properly
    calliope_exceptions.MinimumArgumentException: If the agent cannot be
    deployed properly
  """
  project_id = properties.VALUES.core.project.GetOrFail()

  log.status.Print('Generating connect agent manifest...')

  full_manifest = _GenerateManifest(args,
                                    service_account_key_data,
                                    image_pull_secret_data,
                                    False,
                                    membership_ref, release_track)

  # Generate a manifest file if necessary.
  if args.manifest_output_file:
    try:
      files.WriteFileContents(
          files.ExpandHomeDir(args.manifest_output_file),
          full_manifest,
          private=True)
    except files.Error as e:
      exceptions.Error('could not create manifest file: {}'.format(e))

    log.status.Print(MANIFEST_SAVED_MESSAGE.format(args.manifest_output_file))
    return

  log.status.Print('Deploying GKE Connect agent to cluster...')

  namespace = _GKEConnectNamespace(kube_client, project_id)
  # Delete the ns if necessary
  kube_util.DeleteNamespaceForReinstall(kube_client, namespace)

  # TODO(b/138816749): add check for cluster-admin permissions
  _PurgeAlphaInstaller(kube_client, namespace, project_id)
  # # Create or update the agent install deployment and related resources.
  _, err = kube_client.Apply(full_manifest)
  if err:
    raise exceptions.Error(
        'Failed to apply manifest to cluster: {}'.format(err))
  # TODO(b/131925085): Check connect agent health status.


class NamespaceDeleteOperation(object):
  """An operation that waits for a namespace to be deleted."""

  def __init__(self, namespace, kube_client):
    self.namespace = namespace
    self.kube_client = kube_client
    self.done = False
    self.succeeded = False
    self.error = None

  def __str__(self):
    return '<deleting namespce {}>'.format(self.namespace)

  def Update(self):
    """Updates this operation with the latest namespace deletion status."""
    err = self.kube_client.DeleteNamespace(self.namespace)

    # The first delete request should succeed.
    if not err:
      return

    # If deletion is successful, the delete command will return a NotFound
    # error.
    if 'NotFound' in err:
      self.done = True
      self.succeeded = True
    else:
      self.error = err


def DeleteConnectNamespace(kube_client, args):
  """Delete the namespace in the cluster that contains the connect agent.

  Args:
    kube_client: A Kubernetes Client for the cluster to be registered.
    args: an argparse namespace. All arguments that were provided to this
      command invocation.

  Raises:
    calliope_exceptions.MinimumArgumentException: if a kubeconfig file cannot
      be deduced from the command line flags or environment
  """

  namespace = _GKEConnectNamespace(kube_client,
                                   properties.VALUES.core.project.GetOrFail())
  cleanup_msg = 'Please delete namespace {} manually in your cluster.'.format(
      namespace)

  err = kube_client.DeleteNamespace(namespace)
  if err:
    if 'NotFound' in err:
      # If the namespace was not found, then do not log an error.
      log.status.Print(
          'Namespace [{}] (for context [{}]) did not exist, so it did not '
          'require deletion.'.format(namespace, args.context))
      return
    log.warning(
        'Failed to delete namespace [{}] (for context [{}]): {}. {}'.format(
            namespace, args.context, err, cleanup_msg))
    return


def _GKEConnectNamespace(kube_client, project_id):
  """Returns the namespace into which to install or update the connect agent.

  Connect namespaces are identified by the presence of the hub.gke.io/project
  label. If there is one existing namespace with this label in the cluster, its
  name is returned; otherwise, the default connect namespace is returned.
  If there are multiple namespaces with the
  hub.gke.io/project label, an error is raised.

  Args:
    kube_client: a KubernetesClient
    project_id: A GCP project identifier

  Returns:
    a string, the namespace

  Raises:
    exceptions.Error: if there are multiple Connect namespaces in the cluster
  """
  selector = '{}={}'.format(CONNECT_RESOURCE_LABEL, project_id)
  namespaces = kube_client.NamespacesWithLabelSelector(selector)
  if not namespaces:
    return DEFAULT_NAMESPACE
  if len(namespaces) == 1:
    return namespaces[0]
  raise exceptions.Error(
      'Multiple GKE Connect namespaces in cluster: {}'.format(namespaces))
