// Command healthprobe is a static HTTP health check for images that ship no shell or HTTP
// client. It exits 0 when a GET to the loopback URL in its first argument answers below 400.
// An https URL is verified against the PEM bundle in HEALTHPROBE_CA_FILE, under the name in
// HEALTHPROBE_SERVER_NAME when the certificate does not name the loopback address.
package main

import (
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"os"
	"time"
)

func check(raw string) error {
	return checkWith(raw, os.Getenv("HEALTHPROBE_CA_FILE"), os.Getenv("HEALTHPROBE_SERVER_NAME"))
}

func checkWith(raw, caFile, serverName string) error {
	target, err := url.Parse(raw)
	if err != nil {
		return err
	}
	if target.Scheme != "http" && target.Scheme != "https" {
		return fmt.Errorf("unsupported scheme %q", target.Scheme)
	}
	ip := net.ParseIP(target.Hostname())
	if ip == nil || !ip.IsLoopback() {
		return fmt.Errorf("host %q is not a loopback address", target.Hostname())
	}
	transport := &http.Transport{}
	if target.Scheme == "https" {
		config, err := tlsConfig(caFile, serverName)
		if err != nil {
			return err
		}
		transport.TLSClientConfig = config
	}
	client := &http.Client{
		Timeout:   5 * time.Second,
		Transport: transport,
		CheckRedirect: func(*http.Request, []*http.Request) error {
			return http.ErrUseLastResponse
		},
	}
	response, err := client.Get(target.String())
	if err != nil {
		return err
	}
	defer response.Body.Close()
	if response.StatusCode >= 400 {
		return fmt.Errorf("status %d", response.StatusCode)
	}
	return nil
}

func tlsConfig(caFile, serverName string) (*tls.Config, error) {
	if caFile == "" {
		return nil, errors.New("an https probe needs HEALTHPROBE_CA_FILE")
	}
	bundle, err := os.ReadFile(caFile)
	if err != nil {
		return nil, err
	}
	roots := x509.NewCertPool()
	if !roots.AppendCertsFromPEM(bundle) {
		return nil, fmt.Errorf("%s holds no PEM certificate", caFile)
	}
	return &tls.Config{RootCAs: roots, ServerName: serverName, MinVersion: tls.VersionTLS12}, nil
}

func main() {
	if len(os.Args) != 2 {
		fmt.Fprintln(os.Stderr, "usage: healthprobe http[s]://127.0.0.1:PORT/PATH")
		os.Exit(2)
	}
	if err := check(os.Args[1]); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}
