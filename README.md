# How RootViz works

This is the documentation on how we generate the graphs for https://rootviz.sidnlabs.nl

# How we generate these graphs

We measure the reachability of each Root Server Letter (A--M) by doing the following:

1. Download every `30min` all results from the [RIPE Atlas](https://atlas.ripe.net) Root DNS Measurements:
   * All probes (10k+) send `chaos TXT hostname.bind` to each Root Server Letter _direclty_, bypassing their resolvers
   * For instance, see  [K-ROOT IPv6 measurement](https://atlas.ripe.net/measurements/11301). Other Root Sever Letter IDs can be found [here](https://github.com/SIDN/rootviz/blob/main/root_measurement_ids.json)
      * (they have been used in multiple studies, such as [this one](https://ant.isi.edu/~johnh/PAPERS/Moura16b.pdf)
2. For each DNS response obtained, we look into their `result` variable (see [RIPE Atlas documentation](https://atlas.ripe.net/docs/apis/measurement-result-format#version-5000)):
    ```json
    "result" -- variable content, depending on type of response (array)
    Each element is an associative array consisting of:
    Case: Timeout
    "x" -- "*" (string)
    Case: Error
    "error" -- description of error (string)
    Case: Reply
    "rtt" -- round-trip-time in milliseconds (float)
    "ttl" -- [optional] time-to-live reply if different from ttl in first reply (int)
    "dup" -- [optional] signals that the reply is a duplicate (int)
    ```
3. We count, for each measurement ID, how many unique `prb_id` ([Atlas Probe unique ID](https://atlas.ripe.net/docs/apis/measurement-result-format#version-5000)) had an `timeout`:
   * Timeout means the probe was active by somehow it could not reach the designated IP address
4. We show the results in a time series 


### Details and caveats:

* We know that a single probe is not reliable -- after all, they can be [interecepted, hijacked, and so forth](https://www.sidnlabs.nl/downloads/4Ud373Ww1AWiQwQzvu9WkW/424f24e56fb7f5bcce3cfbb0cbd94b52/Intercept_and_Inject_DNS_Response_Manipulation_in_the_Wild.pdf_)
* However, what we do here is a bit of _crowd sourcing_: if many probes report an issue, therefore it must be something with the server and not the clients
* We were able to spot and confirm with two different Root Sever Letter operators events that happened and last ~ 30mins
   * We can look at the _catchment_ of these probes before an event and see which anycast site likely had issue
* We are working on other metrics, and we will post them soon as other public dashboards
* Contact: `giovane . moura @ sidn .nl `
