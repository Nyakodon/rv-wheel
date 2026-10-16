function snap(v)
    v = v or FindFirstOf("BP_Vehicle_Winnebago_01_C")
    local vel, fwd = v:GetVelocity(), v:GetActorForwardVector()
    local fs = (vel.X * fwd.X + vel.Y * fwd.Y + vel.Z * fwd.Z) / 27.78 -- cm/s -> km/h
    return string.format("fwd=%+6.1fkm/h rpm=%5.0f gear=%s shifter=%s tmode=%s auto=%s torque=%6.1f thr=%.2f brk=%.2f str=%+.2f clutch=%s eng=%s",
        fs, v.RPM, tostring(v.CurrentGear), tostring(v.Shifter), tostring(v.TransmissionMode), tostring(v.AutomaticTransmission),
        v.Torque, v.Throttle, v.Brake, v.Steering, tostring(v.IsClutchDown), tostring(v.EngineRunning))
end
