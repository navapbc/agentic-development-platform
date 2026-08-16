import jenkins.model.Jenkins
import hudson.security.HudsonPrivateSecurityRealm
import hudson.security.AuthorizationStrategy

def instance = Jenkins.get()
instance.setSecurityRealm(new HudsonPrivateSecurityRealm(false))
instance.setAuthorizationStrategy(AuthorizationStrategy.UNSECURED)
instance.setCrumbIssuer(null)
instance.save()
