{{/* vim: set filetype=mustache: */}}

{{- define "chart.image" -}}
{{- printf "%s/%s:%s" .Values.registry .Values.appname .Values.imageTag}}
{{- end -}}

{{/*
Create chart name and version as used by the chart label.
*/}}

{{/*
Common labels
*/}}
{{- define "chart.labels" -}}
helm.sh/chart: {{ .Release.Name }}
{{ include "chart.selectorLabels" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- with .Values.labels }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{/*
Selector labels
*/}}
{{- define "chart.selectorLabels" -}}
app.kubernetes.io/name: {{ .Values.appname }}
{{- end -}}

{{/*
Return the apiVersion of deployment.
*/}}
{{- define "deployment.apiVersion" -}}
{{- if semverCompare "<1.14-0" .Capabilities.KubeVersion.GitVersion -}}
{{- print "extensions/v1beta1" -}}
{{- else if semverCompare ">=1.14-0" .Capabilities.KubeVersion.GitVersion -}}
{{- print "apps/v1" -}}
{{- end -}}
{{- end -}}

{{/*
Return the appropriate apiVersion for cronjob APIs.
*/}}
{{- define "cronjob.apiVersion" -}}
{{- if semverCompare "< 1.8-0" .Capabilities.KubeVersion.GitVersion -}}
{{- print "batch/v2alpha1" }}
{{- else if semverCompare ">=1.8-0" .Capabilities.KubeVersion.GitVersion -}}
{{- print "batch/v1beta1" }}
{{- end -}}
{{- end -}}

{{/*
Return the appropriate apiVersion for ingress.
*/}}
{{- define "ingress.apiVersion" -}}
{{- if semverCompare "<1.14-0" .Capabilities.KubeVersion.GitVersion -}}
{{- print "extensions/v1beta1" -}}
{{- else if semverCompare ">=1.19-0" .Capabilities.KubeVersion.GitVersion -}}
{{- print "networking.k8s.io/v1" -}}
{{- else -}}
{{- print "networking.k8s.io/v1beta1" -}}
{{- end -}}
{{- end -}}

{{/*
Return the appropriate hostAliases
*/}}
{{- define "hostAliases" -}}
- ip: "172.30.4.19"
  hostnames:
    - "cls-ibizl0sz.ccs.tencent-cloud.com"
- ip: "10.22.194.17"
  hostnames:
    - "cls-ndk7otgf.ccs.tencent-cloud.com"
- ip: "10.22.192.13"
  hostnames:
    - "cls-18ny69zr.ccs.tencent-cloud.com"
{{- with $.Values.hostAliases }}
{{ toYaml $.Values.hostAliases }}
{{- end }}
{{- end -}}

{{/*
Return the default env
*/}}
{{- define "defaultEnv" -}}
- name: LAIN_CLUSTER
  value: {{ default "UNKNOWN" $.Values.cluster }}
- name: K8S_NAMESPACE
  value: {{ default "default" $.Values.k8s_namespace }}
- name: IMAGE_TAG
  value: {{ default "UNKNOWN" $.Values.imageTag }}
{{- if hasKey $.Values "env" }}
{{- range $index, $element := $.Values.env }}
- name: {{ $index | quote }}
  value: {{ $element | quote }}
{{- end }}
{{- end }}
{{- end -}}
