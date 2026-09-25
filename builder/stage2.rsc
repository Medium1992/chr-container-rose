# /import runs every top-level line in its own scope, so :local values would
# not survive from one line to the next.  Keep the whole script in one block.
{
    # A freshly bound DHCP lease sometimes drops the first request; retry markers.
    :local mark do={
        :local sent false
        :for i from=1 to=10 do={
            :if (!$sent) do={
                :do {
                    /tool/fetch url=("http://10.0.2.2:{PORT}/marker/" . $1) output=none
                    :set sent true
                } on-error={ :delay 1s }
            }
        }
    }
    /system/scheduler/remove [find name=build-stage2]
    /file/remove [find name=build-stage2.rsc]
    # Startup schedulers fire before DHCP has bound; markers need the network.
    :local t 0
    :while (([:len [/ip/dhcp-client/find where status="bound"]] = 0) && ($t < 120)) do={
        :delay 1s
        :set t ($t + 1)
    }
    :foreach pkg in={"container";"rose-storage"} do={
        :if ([:len [/system/package/find where name=$pkg]] > 0) do={
            $mark ("installed-" . $pkg)
        } else={
            $mark ("error-missing-" . $pkg)
        }
    }
    :onerror e in={
        /system/scheduler/add name=build-reset start-time=startup on-event="/system/scheduler/remove [find name=build-reset]; /system/reset-configuration no-defaults=yes skip-backup=yes"
    } do={
        $mark "error-scheduler"
    }
    $mark "device-mode-pending"
    :onerror e in={
        /system/device-mode/update container=yes traffic-gen=yes
    } do={
        $mark "error-device-mode"
    }
    $mark "device-mode-returned"
}
