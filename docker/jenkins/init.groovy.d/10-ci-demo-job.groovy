import com.tikal.hudson.plugins.notification.Endpoint
import com.tikal.hudson.plugins.notification.HudsonNotificationProperty
import com.tikal.hudson.plugins.notification.Format
import com.tikal.hudson.plugins.notification.Protocol
import hudson.model.ParametersDefinitionProperty
import hudson.model.StringParameterDefinition
import hudson.plugins.git.BranchSpec
import hudson.plugins.git.GitSCM
import hudson.plugins.git.UserRemoteConfig
import jenkins.model.Jenkins
import org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition
import org.jenkinsci.plugins.workflow.job.WorkflowJob

def jobName = "agentic-demo-service-ci"
def repoUrl = "https://github.com/joerobinson-nava/agentic-demo-service.git"
def base = System.getenv("PLATFORM_WEBHOOK_BASE_URL") ?: "http://host.docker.internal:8140"
def callback = base.replaceAll("/\$", "") + "/webhook/ci"

def instance = Jenkins.get()
def existing = instance.getItem(jobName)
if (existing != null) {
    existing.delete()
}
def job = instance.createProject(WorkflowJob, jobName)

job.addProperty(new ParametersDefinitionProperty([
    new StringParameterDefinition("EXECUTION_ID", "", "platform execution id"),
    new StringParameterDefinition("BRANCH", "", "branch under test"),
    new StringParameterDefinition("COMMIT_SHA", "", "commit under test"),
    new StringParameterDefinition("REPO", "agentic-demo-service", "repository slug"),
]))

def scm = new GitSCM(
    [new UserRemoteConfig(repoUrl, null, null, null)],
    [new BranchSpec('${BRANCH}')],
    null,
    null,
    []
)
def definition = new CpsScmFlowDefinition(scm, "Jenkinsfile")
definition.setLightweight(false)
job.setDefinition(definition)

def endpoint = new Endpoint(Protocol.HTTP, callback, "completed", Format.JSON, 30000, 0)
job.addProperty(new HudsonNotificationProperty([endpoint]))

job.save()
