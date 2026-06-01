<?hh

enum class FortGChatScriptParam: IScriptParam extends ScriptParamsSchema {
  ITypedScriptParam<string> action = ScriptArg::String()
    ->description(
      'Action: list_spaces, list_messages, send_message, get_message',
    );

  ITypedScriptParam<?string> space = ScriptOption::String()
    ->shortName('s')
    ->description('Space resource name (e.g. spaces/AAAA1234)');

  ITypedScriptParam<?string> message = ScriptOption::String()
    ->shortName('m')
    ->description('Message text to send');

  ITypedScriptParam<?string> message_name = ScriptOption::String()
    ->description('Message resource name (for get_message)');

  ITypedScriptParam<?int> limit = ScriptOption::Int()
    ->shortName('n')
    ->description('Number of results to fetch (default 25)');

  ITypedScriptParam<bool> json = ScriptOption::Exists()
    ->shortName('j')
    ->description('Output as JSON (for machine parsing)');
}

<<Oncalls('fort')>>
final class FortGChatScript
  extends ScriptController
  implements IOPEScriptController {
  use TScriptControllerWithMyVC;

  const type TParams = FortGChatScriptParam;

  <<__Override>>
  protected static function getDescription(): string {
    return 'Fort GChat bot: list spaces, read/send messages';
  }

  <<__Override>>
  protected async function genRun(): Awaitable<ScriptReturnCode> {
    $vc = await $this->genViewerContext();
    $bot = GoogleChatBot::withoutBotID_RESTRICTED(#TEST);
    $client = $bot->actingAsUserVC($vc);

    $action = $this->getParam(#action);

    try {
      switch ($action) {
        case 'list_spaces':
          return await $this->genListSpaces($client);
        case 'list_messages':
          return await $this->genListMessages($client);
        case 'send_message':
          return await $this->genSendMessage($client);
        case 'get_message':
          return await $this->genGetMessage($client);
        default:
          return $this->error(
            'Unknown action: '.$action.
            '. Valid: list_spaces, list_messages, send_message, get_message',
          );
      }
    } catch (\Exception $ex) {
      return $this->error($ex->getMessage());
    }
  }

  private function isJson(): bool {
    return $this->getParam(#json);
  }

  private function outputJson(dict<string, mixed> $data): void {
    ScriptEcho::printlnf(
      '%s',
      \json_encode(
        dict['success' => true, 'data' => $data],
        \JSON_PRETTY_PRINT,
      ),
    );
  }

  private function error(string $msg): ScriptReturnCode {
    if ($this->isJson()) {
      ScriptEcho::printlnf(
        '%s',
        \json_encode(dict['success' => false, 'error' => $msg]),
      );
    } else {
      ScriptEcho::printlnf('Error: %s', $msg);
    }
    return ScriptReturnCode::FAILURE;
  }

  private async function genListSpaces(
    GoogleChatClient $client,
  ): Awaitable<ScriptReturnCode> {
    $page_size = $this->getParam(#limit) ?? 50;
    $response = await $client->genListSpaces(
      shape('page_size' => $page_size),
    );

    $spaces = vec[];
    foreach ($response->getSpaces() as $space) {
      $space_type = $space->getSpaceType();
      $entry = dict[
        'name' => $space->getName()->toString(),
        'display_name' => $space->getDisplayName() ?? '',
        'space_type' => $space_type is nonnull ? (string)$space_type : 'unknown',
      ];
      $spaces[] = $entry;
    }

    if ($this->isJson()) {
      $this->outputJson(dict['spaces' => $spaces]);
    } else {
      foreach ($spaces as $s) {
        $display = $s['display_name'];
        ScriptEcho::printlnf(
          '%s  |  %s  |  %s',
          $s['name'],
          $display === '' ? '(DM)' : $display,
          $s['space_type'],
        );
      }
    }

    return ScriptReturnCode::SUCCESS;
  }

  private async function genListMessages(
    GoogleChatClient $client,
  ): Awaitable<ScriptReturnCode> {
    $space_name_str = $this->getParam(#space);
    if ($space_name_str is null) {
      return $this->error('--space is required for list_messages');
    }

    $space_name = GoogleChatSpaceResourceName::fromName($space_name_str);
    $page_size = $this->getParam(#limit) ?? 25;
    $response = await $client->genListMessages(
      $space_name,
      shape('page_size' => $page_size),
    );

    $messages = vec[];
    foreach ($response->getMessages() as $msg) {
      $messages[] = dict[
        'name' => $msg->getName()->toString(),
        'text' =>
          $msg->getPlainText_REMOVES_MARKUP_AND_INLINE_LINKS() ?? '',
        'sender' => $msg->getSender()?->getDisplayName() ?? '',
        'create_time' => $msg->getCreateTime()?->toString() ?? '',
      ];
    }

    if ($this->isJson()) {
      $this->outputJson(dict['messages' => $messages]);
    } else {
      foreach ($messages as $m) {
        $sender = $m['sender'];
        ScriptEcho::printlnf(
          '[%s] %s: %s',
          $m['create_time'],
          $sender === '' ? '(unknown)' : $sender,
          $m['text'],
        );
      }
    }

    return ScriptReturnCode::SUCCESS;
  }

  private async function genSendMessage(
    GoogleChatClient $client,
  ): Awaitable<ScriptReturnCode> {
    $space_name_str = $this->getParam(#space);
    $message_text = $this->getParam(#message);

    if ($space_name_str is null || $message_text is null) {
      return $this->error(
        '--space and --message are required for send_message',
      );
    }

    $space_name = GoogleChatSpaceResourceName::fromName($space_name_str);
    $sent = await $client->genSendMessage(
      $space_name,
      shape('text' => $message_text),
    );

    $msg_name = $sent->getName()->toString();
    if ($this->isJson()) {
      $this->outputJson(dict['message_name' => $msg_name]);
    } else {
      ScriptEcho::printlnf('Sent: %s', $msg_name);
    }

    return ScriptReturnCode::SUCCESS;
  }

  private async function genGetMessage(
    GoogleChatClient $client,
  ): Awaitable<ScriptReturnCode> {
    $message_name_str = $this->getParam(#message_name);
    if ($message_name_str is null) {
      return $this->error('--message-name is required for get_message');
    }

    $message_name =
      GoogleChatSpaceMessageResourceName::fromName($message_name_str);
    $msg = await $client->genGetMessage($message_name);

    $result = dict[
      'name' => $msg->getName()->toString(),
      'text' =>
        $msg->getPlainText_REMOVES_MARKUP_AND_INLINE_LINKS() ?? '',
      'sender' => $msg->getSender()?->getDisplayName() ?? '',
      'create_time' => $msg->getCreateTime()?->toString() ?? '',
    ];

    if ($this->isJson()) {
      $this->outputJson(dict['message' => $result]);
    } else {
      $sender = $result['sender'];
      ScriptEcho::printlnf(
        '[%s] %s: %s',
        $result['create_time'],
        $sender === '' ? '(unknown)' : $sender,
        $result['text'],
      );
    }

    return ScriptReturnCode::SUCCESS;
  }
}
