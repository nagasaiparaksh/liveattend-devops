// Job DSL - both pipelines are created from code and read their Jenkinsfile from GitHub.
def repo = System.getenv('LA_REPO_URL')

pipelineJob('LiveAttend-Deploy') {
  description('GitHub -> tests -> Docker image -> Trivy scan -> Docker Hub -> Kubernetes (Minikube) -> optionally AWS (Terraform + Ansible) -> smoke tests. Runs automatically on every git push.')
  logRotator { numToKeep(30) }
  properties { pipelineTriggers { triggers { pollSCM { scmpoll_spec('H/2 * * * *') } } } }
  definition {
    cpsScm {
      scm { git { remote { url(repo) }; branch('*/main') } }
      scriptPath('Jenkinsfile')
      lightweight(false)
    }
  }
}

pipelineJob('LiveAttend-Destroy') {
  description('Removes the AWS resources Terraform created and/or the Kubernetes namespace. Type destroy to confirm.')
  logRotator { numToKeep(10) }
  definition {
    cpsScm {
      scm { git { remote { url(repo) }; branch('*/main') } }
      scriptPath('Jenkinsfile.destroy')
      lightweight(false)
    }
  }
}
