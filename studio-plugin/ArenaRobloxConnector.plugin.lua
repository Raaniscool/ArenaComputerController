-- Arena Roblox Connector: read-only overview plus tightly constrained user-approved operations.
-- No assistant-generated JavaScript or Luau is accepted or executed.
local HttpService = game:GetService("HttpService")
local RunService = game:GetService("RunService")

local BASE_URL = "http://127.0.0.1:4317"
local TOKEN_SETTING = "ConnectorToken"
local HEARTBEAT_INTERVAL = 5
local ALLOWED_SERVICES = {
    "Workspace", "ReplicatedStorage", "ServerStorage", "ServerScriptService",
    "StarterGui", "StarterPack", "StarterPlayer", "Lighting", "SoundService",
}

local toolbar = plugin:CreateToolbar("Arena Connector")
local toggleButton = toolbar:CreateButton("ArenaConnector", "Show connector status", "")
toggleButton.ClickableWhenViewportHidden = true

local widgetInfo = DockWidgetPluginGuiInfo.new(Enum.InitialDockState.Float, false, false, 440, 560, 380, 480)
local widget = plugin:CreateDockWidgetPluginGui("ArenaRobloxConnectorWidget", widgetInfo)
widget.Title = "Arena Roblox Connector"

local frame = Instance.new("Frame")
frame.Size = UDim2.fromScale(1, 1)
frame.BackgroundColor3 = Color3.fromRGB(35, 38, 45)
frame.BorderSizePixel = 0
frame.Parent = widget

local function makeLabel(text, position, size, textSize)
    local item = Instance.new("TextLabel")
    item.Position = position
    item.Size = size
    item.BackgroundTransparency = 1
    item.TextColor3 = Color3.fromRGB(235, 238, 245)
    item.TextXAlignment = Enum.TextXAlignment.Left
    item.TextYAlignment = Enum.TextYAlignment.Top
    item.TextWrapped = true
    item.Font = Enum.Font.SourceSans
    item.TextSize = textSize
    item.Text = text
    item.Parent = frame
    return item
end

makeLabel("Studio token (plugin settings are not an encrypted vault):", UDim2.new(0, 12, 0, 8), UDim2.new(1, -24, 0, 24), 15)
local tokenBox = Instance.new("TextBox")
tokenBox.Position = UDim2.new(0, 12, 0, 32)
tokenBox.Size = UDim2.new(1, -24, 0, 32)
tokenBox.BackgroundColor3 = Color3.fromRGB(54, 58, 68)
tokenBox.TextColor3 = Color3.new(1, 1, 1)
tokenBox.PlaceholderText = "Paste CONNECTOR_TOKEN (Studio)"
tokenBox.ClearTextOnFocus = false
tokenBox.TextXAlignment = Enum.TextXAlignment.Left
tokenBox.Font = Enum.Font.Code
tokenBox.TextSize = 14
tokenBox.Text = plugin:GetSetting(TOKEN_SETTING) or ""
tokenBox.Parent = frame

local statusLabel = makeLabel("Not connected.", UDim2.new(0, 12, 0, 72), UDim2.new(1, -24, 0, 42), 15)
local overviewLabel = makeLabel("Project overview appears after a successful heartbeat.", UDim2.new(0, 12, 0, 118), UDim2.new(1, -24, 0, 116), 14)
local approvalScroll = Instance.new("ScrollingFrame")
approvalScroll.Name = "ApprovalPreviewScroll"
approvalScroll.Position = UDim2.new(0, 12, 0, 238)
approvalScroll.Size = UDim2.new(1, -24, 1, -338)
approvalScroll.BackgroundColor3 = Color3.fromRGB(43, 47, 56)
approvalScroll.BackgroundTransparency = 0
approvalScroll.BorderSizePixel = 0
approvalScroll.ScrollBarThickness = 8
approvalScroll.ScrollingDirection = Enum.ScrollingDirection.Y
approvalScroll.AutomaticCanvasSize = Enum.AutomaticSize.Y
approvalScroll.CanvasSize = UDim2.new(0, 0, 0, 0)
approvalScroll.Parent = frame
local approvalLabel = Instance.new("TextLabel")
approvalLabel.Name = "ApprovalPreviewText"
approvalLabel.Position = UDim2.new(0, 8, 0, 8)
approvalLabel.Size = UDim2.new(1, -24, 0, 0)
approvalLabel.AutomaticSize = Enum.AutomaticSize.Y
approvalLabel.BackgroundTransparency = 1
approvalLabel.TextColor3 = Color3.fromRGB(235, 238, 245)
approvalLabel.TextXAlignment = Enum.TextXAlignment.Left
approvalLabel.TextYAlignment = Enum.TextYAlignment.Top
approvalLabel.TextWrapped = true
approvalLabel.Font = Enum.Font.SourceSans
approvalLabel.TextSize = 14
approvalLabel.Text = "No pending Studio operation."
approvalLabel.Parent = approvalScroll
local rejectButton = Instance.new("TextButton")
rejectButton.Position = UDim2.new(0, 12, 1, -90)
rejectButton.Size = UDim2.new(0.5, -18, 0, 34)
rejectButton.BackgroundColor3 = Color3.fromRGB(150, 65, 65)
rejectButton.TextColor3 = Color3.new(1, 1, 1)
rejectButton.Text = "Reject"
rejectButton.Font = Enum.Font.SourceSansBold
rejectButton.TextSize = 16
rejectButton.Visible = false
rejectButton.Parent = frame
local approveButton = Instance.new("TextButton")
approveButton.Position = UDim2.new(0.5, 6, 1, -90)
approveButton.Size = UDim2.new(0.5, -18, 0, 34)
approveButton.BackgroundColor3 = Color3.fromRGB(49, 132, 83)
approveButton.TextColor3 = Color3.new(1, 1, 1)
approveButton.Text = "Approve Folder"
approveButton.Font = Enum.Font.SourceSansBold
approveButton.TextSize = 16
approveButton.Visible = false
approveButton.Parent = frame
local checkButton = Instance.new("TextButton")
checkButton.Position = UDim2.new(0, 12, 1, -48)
checkButton.Size = UDim2.new(1, -24, 0, 36)
checkButton.BackgroundColor3 = Color3.fromRGB(54, 112, 190)
checkButton.TextColor3 = Color3.new(1, 1, 1)
checkButton.Text = "Connect / refresh overview"
checkButton.Font = Enum.Font.SourceSansBold
checkButton.TextSize = 16
checkButton.Parent = frame

toggleButton.Click:Connect(function() widget.Enabled = not widget.Enabled end)

local sessionId = nil
local cycleBusy = false
local heartbeatLoopStarted = false
local currentRequest = nil
local actionBusy = false
local function validOperationName(value)
    return type(value) == "string"
        and #value >= 1 and #value <= 40
        and value == string.match(value, "^%s*(.-)%s*$")
        and string.match(value, "^[A-Za-z0-9 _%-]+$") ~= nil
end
local function setStatus(text, color)
    statusLabel.Text = text
    statusLabel.TextColor3 = color or Color3.fromRGB(235, 238, 245)
end
local function setApproval(operation, note)
    local previousRequestId = currentRequest and currentRequest.requestId
    currentRequest = operation
    if operation and previousRequestId ~= operation.requestId then
        approvalScroll.CanvasPosition = Vector2.new(0, 0)
    end
    if operation then
        if operation.operation == "create_test_folder" then
            approveButton.Text = "Approve Folder"
            approvalLabel.Text = "Arena requests: create Folder \"" .. operation.name .. "\" under Workspace.\nReview the name and click Approve or Reject. Expires shortly."
        elseif operation.operation == "create_part" then
            approveButton.Text = "Approve Part"
            local p, z, c = operation.position, operation.size, operation.color
            approvalLabel.Text = "Arena requests: create Part \"" .. operation.name .. "\" under Workspace\nPosition: (" .. tostring(p.x) .. ", " .. tostring(p.y) .. ", " .. tostring(p.z) .. ") studs\nSize: (" .. tostring(z.x) .. ", " .. tostring(z.y) .. ", " .. tostring(z.z) .. ") studs\nRGB: (" .. tostring(c.x) .. ", " .. tostring(c.y) .. ", " .. tostring(c.z) .. ") | Material: " .. operation.material .. "\nAnchored: " .. tostring(operation.anchored) .. " | CanCollide: " .. tostring(operation.canCollide) .. "\nReview exact properties; approve or reject. Expires shortly."
        else
            approvalLabel.Text = "Unsupported operation. No change will be made."
            rejectButton.Visible = false
            approveButton.Visible = false
            return
        end
        rejectButton.Visible = true
        approveButton.Visible = true
        rejectButton.Active = not actionBusy
        approveButton.Active = not actionBusy
        rejectButton.AutoButtonColor = not actionBusy
        approveButton.AutoButtonColor = not actionBusy
    elseif note then
        approvalLabel.Text = note
        rejectButton.Visible = false
        approveButton.Visible = false
    else
        approvalLabel.Text = "No pending Studio operation. Every change requires approval here."
        rejectButton.Visible = false
        approveButton.Visible = false
    end
end
local function request(method, path, token, body)
    local headers = { ["Authorization"] = "Bearer " .. token }
    local options = { Url = BASE_URL .. path, Method = method, Headers = headers }
    if body ~= nil then
        headers["Content-Type"] = "application/json"
        options.Body = HttpService:JSONEncode(body)
    end
    return HttpService:RequestAsync(options)
end
local function decodeResponse(response, expectedCode)
    if response.StatusCode == 401 then error("Studio authentication failed (401): check CONNECTOR_TOKEN.") end
    if response.StatusCode == 409 then error("Session or request expired/replaced (409).") end
    if response.StatusCode ~= expectedCode or not response.Success then
        error("Connector returned HTTP " .. tostring(response.StatusCode) .. ".")
    end
    local ok, decoded = pcall(function() return HttpService:JSONDecode(response.Body) end)
    if not ok or type(decoded) ~= "table" or decoded.ok ~= true then
        error("Connector returned invalid JSON or an unexpected response.")
    end
    return decoded
end
local function collectOverview()
    local services = {}
    for _, serviceName in ipairs(ALLOWED_SERVICES) do
        local service = game:GetService(serviceName)
        table.insert(services, {
            name = serviceName,
            className = service.ClassName,
            directChildCount = #service:GetChildren(),
        })
    end
    return {
        metadata = {
            placeId = game.PlaceId,
            universeId = game.GameId,
            isStudio = RunService:IsStudio(),
        },
        services = services,
    }
end
local function register(token)
    local response = request("POST", "/v1/session/register", token)
    local payload = decodeResponse(response, 201)
    if type(payload.sessionId) ~= "string" or #payload.sessionId ~= 36 then
        error("Connector registration response did not contain a valid session id.")
    end
    sessionId = payload.sessionId
end
local function validateOverview(payload, expectedSession)
    if type(payload.sessionId) ~= "string" or payload.sessionId ~= expectedSession then return false end
    local project = payload.project
    if type(project) ~= "table" or type(project.metadata) ~= "table" or type(project.services) ~= "table" then return false end
    local metadata = project.metadata
    if type(metadata.placeId) ~= "number" or type(metadata.universeId) ~= "number" or type(metadata.isStudio) ~= "boolean" then return false end
    if #project.services > #ALLOWED_SERVICES then return false end
    for _, service in ipairs(project.services) do
        if type(service) ~= "table" or type(service.name) ~= "string"
            or type(service.className) ~= "string" or type(service.directChildCount) ~= "number" then return false end
    end
    return true
end
local function reportOperation(operation, token, ok, message, createdName)
    local response = request("POST", "/v1/operations/" .. operation.requestId .. "/result", token, {
        sessionId = sessionId,
        approvalToken = operation.approvalToken,
        ok = ok,
        message = string.sub(message, 1, 180),
        createdName = createdName,
    })
    return decodeResponse(response, 200)
end
local function applyApprovedOperation(operation, token)
    local existingByRequest = nil
    for _, child in ipairs(workspace:GetChildren()) do
        if child:GetAttribute("ArenaConnectorRequestId") == operation.requestId then
            existingByRequest = child
            break
        end
    end
    if existingByRequest then
        local expectedClass = operation.operation == "create_test_folder" and "Folder" or "Part"
        local matches = existingByRequest:IsA(expectedClass) and existingByRequest.Name == operation.name
        if matches and operation.operation == "create_part" then
            matches = existingByRequest.Position == Vector3.new(operation.position.x, operation.position.y, operation.position.z)
                and existingByRequest.Size == Vector3.new(operation.size.x, operation.size.y, operation.size.z)
                and existingByRequest.Color == Color3.fromRGB(operation.color.x, operation.color.y, operation.color.z)
                and existingByRequest.Material == Enum.Material[operation.material]
                and existingByRequest.Anchored == operation.anchored and existingByRequest.CanCollide == operation.canCollide
        end
        if matches then
            reportOperation(operation, token, true, expectedClass .. " already created for this request.", operation.name)
            setApproval(nil, "Completed: Workspace/" .. operation.name .. " (retry safely recognized).")
            return
        end
        reportOperation(operation, token, false, "Request marker exists on an unexpected object.", nil)
        setApproval(nil, "Rejected: request marker did not match the expected object for this request.")
        return
    end
    if workspace:FindFirstChild(operation.name) then
        reportOperation(operation, token, false, "A Workspace child with this name already exists.", nil)
        setApproval(nil, "Not created: a Workspace child already has that name.")
        return
    end
    local className = operation.operation == "create_test_folder" and "Folder" or "Part"
    local instance = Instance.new(className)
    instance.Name = operation.name
    if className == "Part" then
        instance.Position = Vector3.new(operation.position.x, operation.position.y, operation.position.z)
        instance.Size = Vector3.new(operation.size.x, operation.size.y, operation.size.z)
        instance.Color = Color3.fromRGB(operation.color.x, operation.color.y, operation.color.z)
        instance.Material = Enum.Material[operation.material]
        instance.Anchored = operation.anchored
        instance.CanCollide = operation.canCollide
    end
    instance:SetAttribute("ArenaConnectorRequestId", operation.requestId)
    instance.Parent = workspace
    reportOperation(operation, token, true, "Created one " .. className .. " under Workspace.", operation.name)
    setApproval(nil, "Completed: created Workspace/" .. operation.name .. ".")
end
local function recoverApproved(operation, token)
    if actionBusy then return end
    actionBusy = true
    setApproval(nil, "Previously approved: safely completing or confirming the same request…")
    task.spawn(function()
        local ok, errorMessage = pcall(function() applyApprovedOperation(operation, token) end)
        if not ok then
            setApproval(nil, "Approved request needs retry: " .. tostring(errorMessage))
        end
        actionBusy = false
    end)
end
local function decide(operation, decision)
    if actionBusy or not operation or not sessionId then return end
    actionBusy = true
    approveButton.Active = false
    rejectButton.Active = false
    local token = tokenBox.Text
    task.spawn(function()
        local ok, errorMessage = pcall(function()
            local response = request("POST", "/v1/operations/" .. operation.requestId .. "/decision", token, {
                sessionId = sessionId,
                approvalToken = operation.approvalToken,
                decision = decision,
            })
            local result = decodeResponse(response, 200)
            if decision == "approve" and result.approved == true
                and result.operation == operation.operation and result.name == operation.name then
                applyApprovedOperation(operation, token)
            elseif decision == "reject" and result.approved == false then
                setApproval(nil, "Rejected: no Studio change was made.")
            else
                error("Connector returned an unexpected approval result.")
            end
        end)
        if not ok then
            setApproval(operation, "Approval request failed: " .. tostring(errorMessage))
        end
        actionBusy = false
        if currentRequest then
            approveButton.Active = true
            rejectButton.Active = true
        end
    end)
end
approveButton.Activated:Connect(function()
    if currentRequest then decide(currentRequest, "approve") end
end)
rejectButton.Activated:Connect(function()
    if currentRequest then decide(currentRequest, "reject") end
end)
local function refreshCycle()
    if cycleBusy then return end
    local token = tokenBox.Text
    if #token < 32 then
        sessionId = nil
        setStatus("Studio token required (at least 32 characters). Start the connector and enter CONNECTOR_TOKEN.", Color3.fromRGB(255, 190, 100))
        return
    end
    cycleBusy = true
    task.spawn(function()
        local ok, errorMessage = pcall(function()
            if not sessionId then register(token) end
            local currentSession = sessionId
            local heartbeatResponse = request("POST", "/v1/session/heartbeat", token, {
                sessionId = currentSession,
                overview = collectOverview(),
            })
            local heartbeat = decodeResponse(heartbeatResponse, 200)
            if heartbeat.sessionId ~= currentSession or heartbeat.connected ~= true then
                error("Connector did not confirm this Studio session heartbeat.")
            end
            local pending = heartbeat.pendingOperation
            if pending ~= nil then
                if type(pending.requestId) ~= "string" or (pending.operation ~= "create_test_folder" and pending.operation ~= "create_part")
                    or not validOperationName(pending.name)
                    or (pending.status ~= "pending" and pending.status ~= "approved")
                    or type(pending.approvalToken) ~= "string" or #pending.approvalToken < 20 then
                    error("Connector returned an invalid pending operation.")
                end
                if pending.operation == "create_part" then
                    local function vectorInRange(value, low, high, integer)
                        if type(value) ~= "table" then return false end
                        local keys = 0
                        for key in pairs(value) do if key == "x" or key == "y" or key == "z" then keys += 1 else return false end end
                        if keys ~= 3 then return false end
                        for _, axis in ipairs({"x", "y", "z"}) do
                            local number = value[axis]
                            if type(number) ~= "number" or number ~= number or number == math.huge or number == -math.huge or number < low or number > high or (integer and number % 1 ~= 0) then return false end
                        end
                        return true
                    end
                    local allowedMaterials = {Plastic=true, SmoothPlastic=true, Wood=true, WoodPlanks=true, Concrete=true, Brick=true, Metal=true, Grass=true, Ice=true, Glass=true, Neon=true, Sand=true, Slate=true, Marble=true}
                    if not vectorInRange(pending.position, -10000, 10000, false) or not vectorInRange(pending.size, 0.1, 512, false)
                        or not vectorInRange(pending.color, 0, 255, true) or not allowedMaterials[pending.material]
                        or type(pending.anchored) ~= "boolean" or type(pending.canCollide) ~= "boolean" then
                        error("Connector returned invalid Part properties.")
                    end
                end
                if pending.status == "approved" then
                    recoverApproved(pending, token)
                elseif not actionBusy and (not currentRequest or currentRequest.requestId ~= pending.requestId) then
                    setApproval(pending)
                elseif not actionBusy then
                    setApproval(pending)
                end
            elseif not actionBusy and currentRequest == nil then
                setApproval(nil)
            end
            local overviewResponse = request("GET", "/v1/project/overview", token)
            local overview = decodeResponse(overviewResponse, 200)
            if not validateOverview(overview, currentSession) then
                error("Connector returned an invalid or mismatched project overview.")
            end
            local metadata = overview.project.metadata
            local lines = {
                "Session: " .. string.sub(currentSession, 1, 8),
                "Place ID: " .. tostring(metadata.placeId) .. "   Universe ID: " .. tostring(metadata.universeId),
                "Is Studio: " .. tostring(metadata.isStudio),
                "Direct children (selected services):",
            }
            for _, service in ipairs(overview.project.services) do
                table.insert(lines, service.name .. " [" .. service.className .. "]: " .. tostring(service.directChildCount))
            end
            overviewLabel.Text = table.concat(lines, "\n")
            setStatus("Connected — heartbeat and read-only overview received.", Color3.fromRGB(130, 230, 150))
        end)
        if not ok then
            sessionId = nil
            setStatus("Disconnected / request failed: " .. tostring(errorMessage), Color3.fromRGB(255, 130, 130))
        end
        cycleBusy = false
    end)
end

checkButton.Activated:Connect(function()
    local token = tokenBox.Text
    if #token < 32 then
        setStatus("Token required (at least 32 characters).", Color3.fromRGB(255, 190, 100))
        return
    end
    plugin:SetSetting(TOKEN_SETTING, token)
    refreshCycle()
    if not heartbeatLoopStarted then
        heartbeatLoopStarted = true
        task.spawn(function()
            while heartbeatLoopStarted do
                task.wait(HEARTBEAT_INTERVAL)
                if heartbeatLoopStarted then refreshCycle() end
            end
        end)
    end
end)
