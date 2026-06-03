from typing import ClassVar

from textual.message import Message

from rhizome.app.resource_viewer import ResourceViewerVM
from rhizome.tui.widgets.shared.choices_list import ChoiceList

class ResourceViewerActions(ChoiceList[ResourceViewerVM]):
    ORIENTATION = "horizontal"

    CHOICES: ClassVar[dict[str, str]] = {
        "Set Topic": "_set_topic",
        "Create Resource": "_create_resource",
        "Link Resources": "_link_resources",
    }

    LEAD = "Actions\n"

    class SelectTopic(Message):
        """Request to open the topic selector."""

    class CreateResource(Message):
        """Request to create a resource."""

    class LinkResources(Message):
        """Request to link resources."""

    def _set_topic(self) -> None:
        self.post_message(self.SelectTopic())
    
    def _create_resource(self) -> None:
        self.post_message(self.CreateResource())

    def _link_resources(self) -> None:
        self.post_message(self.LinkResources())