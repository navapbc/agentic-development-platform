// Command tls-sidecar gives a task behind the internal load balancer a TLS endpoint whose key
// never leaves the task. The load balancer re-encrypts to the task and does not verify the
// certificate, so each certificate is self-signed and issued when the task starts.
//
//	tls-sidecar proxy -listen :3443 -upstream http://127.0.0.1:3000 [-cert-dir DIR]
//	tls-sidecar issue -out DIR [-owner UID:GID]
//
// proxy serves TLS on -listen and forwards to -upstream, writing its certificate to -cert-dir
// so a health probe can verify it. issue writes cert.pem and key.pem to -out for a container
// that reads its certificate from files, and exits.
package main

import (
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"errors"
	"flag"
	"fmt"
	"math/big"
	"net"
	"net/http"
	"net/http/httputil"
	"net/url"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

const (
	certificateFile = "cert.pem"
	keyFile         = "key.pem"
	validity        = 397 * 24 * time.Hour
)

type material struct {
	certificatePEM []byte
	keyPEM         []byte
}

func issue(now time.Time) (material, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return material{}, err
	}
	serial, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 127))
	if err != nil {
		return material{}, err
	}
	hostname, _ := os.Hostname()
	names := []string{"localhost"}
	if hostname != "" {
		names = append(names, hostname)
	}
	template := &x509.Certificate{
		SerialNumber:          serial,
		Subject:               pkix.Name{CommonName: names[len(names)-1]},
		NotBefore:             now.Add(-5 * time.Minute),
		NotAfter:              now.Add(validity),
		KeyUsage:              x509.KeyUsageDigitalSignature,
		ExtKeyUsage:           []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		BasicConstraintsValid: true,
		DNSNames:              names,
		IPAddresses:           []net.IP{net.IPv4(127, 0, 0, 1)},
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		return material{}, err
	}
	keyDER, err := x509.MarshalPKCS8PrivateKey(key)
	if err != nil {
		return material{}, err
	}
	return material{
		certificatePEM: pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: der}),
		keyPEM:         pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: keyDER}),
	}, nil
}

func write(dir string, issued material, withKey bool, uid, gid int) error {
	if err := os.MkdirAll(dir, 0o755); err != nil {
		return err
	}
	files := map[string][]byte{certificateFile: issued.certificatePEM}
	if withKey {
		files[keyFile] = issued.keyPEM
	}
	for name, content := range files {
		mode := os.FileMode(0o644)
		if name == keyFile {
			mode = 0o600
		}
		path := filepath.Join(dir, name)
		if err := os.WriteFile(path, content, mode); err != nil {
			return err
		}
		if err := os.Chmod(path, mode); err != nil {
			return err
		}
		if uid >= 0 {
			if err := os.Chown(path, uid, gid); err != nil {
				return err
			}
		}
	}
	return nil
}

func parseOwner(owner string) (int, int, error) {
	if owner == "" {
		return -1, -1, nil
	}
	user, group, found := strings.Cut(owner, ":")
	if !found {
		group = user
	}
	uid, err := strconv.Atoi(user)
	if err != nil {
		return 0, 0, fmt.Errorf("owner %q is not UID:GID", owner)
	}
	gid, err := strconv.Atoi(group)
	if err != nil {
		return 0, 0, fmt.Errorf("owner %q is not UID:GID", owner)
	}
	return uid, gid, nil
}

func proxyHandler(upstream *url.URL) http.Handler {
	return &httputil.ReverseProxy{
		Rewrite: func(request *httputil.ProxyRequest) {
			request.SetURL(upstream)
			request.Out.Host = request.In.Host
			if forwarded, ok := request.In.Header["X-Forwarded-For"]; ok {
				request.Out.Header["X-Forwarded-For"] = forwarded
			}
			request.SetXForwarded()
		},
	}
}

func newServer(listen string, upstream *url.URL, issued material) (*http.Server, error) {
	certificate, err := tls.X509KeyPair(issued.certificatePEM, issued.keyPEM)
	if err != nil {
		return nil, err
	}
	return &http.Server{
		Addr:              listen,
		Handler:           proxyHandler(upstream),
		ReadHeaderTimeout: 10 * time.Second,
		IdleTimeout:       2 * time.Minute,
		TLSConfig: &tls.Config{
			MinVersion:   tls.VersionTLS12,
			Certificates: []tls.Certificate{certificate},
		},
	}, nil
}

func run(args []string) error {
	if len(args) == 0 {
		return errors.New("usage: tls-sidecar proxy|issue [flags]")
	}
	switch args[0] {
	case "issue":
		flags := flag.NewFlagSet("issue", flag.ContinueOnError)
		out := flags.String("out", "", "directory for cert.pem and key.pem")
		owner := flags.String("owner", "", "UID:GID that owns the written files")
		if err := flags.Parse(args[1:]); err != nil {
			return err
		}
		if *out == "" {
			return errors.New("issue needs -out")
		}
		uid, gid, err := parseOwner(*owner)
		if err != nil {
			return err
		}
		issued, err := issue(time.Now())
		if err != nil {
			return err
		}
		return write(*out, issued, true, uid, gid)
	case "proxy":
		flags := flag.NewFlagSet("proxy", flag.ContinueOnError)
		listen := flags.String("listen", ":3443", "TLS listen address")
		rawUpstream := flags.String("upstream", "", "plain HTTP URL to forward to")
		certDir := flags.String("cert-dir", "", "directory to write cert.pem to for the health probe")
		if err := flags.Parse(args[1:]); err != nil {
			return err
		}
		upstream, err := url.Parse(*rawUpstream)
		if err != nil || upstream.Scheme != "http" || upstream.Host == "" {
			return fmt.Errorf("upstream %q is not an http URL", *rawUpstream)
		}
		issued, err := issue(time.Now())
		if err != nil {
			return err
		}
		if *certDir != "" {
			if err := write(*certDir, issued, false, -1, -1); err != nil {
				return err
			}
		}
		server, err := newServer(*listen, upstream, issued)
		if err != nil {
			return err
		}
		return server.ListenAndServeTLS("", "")
	default:
		return fmt.Errorf("unknown mode %q, expected proxy or issue", args[0])
	}
}

func main() {
	if err := run(os.Args[1:]); err != nil {
		fmt.Fprintln(os.Stderr, "tls-sidecar:", err)
		os.Exit(1)
	}
}
